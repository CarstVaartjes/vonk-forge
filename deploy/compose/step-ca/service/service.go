package main

import (
	"bytes"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"errors"
	"io"
	"net/http"

	"github.com/smallstep/certificates/api"
	"github.com/smallstep/certificates/authority"
	"github.com/smallstep/certificates/authority/provisioner"
	"github.com/smallstep/certificates/db"
)

type Service struct {
	Authority *authority.Authority
	CAS       *JournalCAS
	Journal   *JournalDB
	Policy    Policy
}
type signRequest struct {
	CSR     string          `json:"csr"`
	OTT     string          `json:"ott"`
	Request json.RawMessage `json:"request"`
	Mode    string          `json:"mode"`
}
type issuedReply struct {
	State   string   `json:"state"`
	Request Binding  `json:"request"`
	CRT     string   `json:"crt"`
	CA      string   `json:"ca"`
	Chain   []string `json:"certChain"`
}
type pendingReply struct {
	State   string  `json:"state"`
	Request Binding `json:"request"`
	Reason  string  `json:"reason_code"`
}
type absentReply struct {
	State   string  `json:"state"`
	Request Binding `json:"request"`
}

func NewService(auth *authority.Authority, cas *JournalCAS, journal *JournalDB, policy Policy) *Service {
	return &Service{auth, cas, journal, policy}
}
func jsonReply(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
func failReply(w http.ResponseWriter, err error) {
	var typed *fault
	if errors.As(err, &typed) {
		jsonReply(w, typed.status, map[string]string{"reason_code": typed.code})
		return
	}
	jsonReply(w, http.StatusServiceUnavailable, map[string]string{"reason_code": "certificate.issuance_unavailable"})
}

func (s *Service) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	r = r.WithContext(authority.NewContext(r.Context(), s.Authority))
	switch {
	case r.Method == http.MethodGet && r.URL.Path == "/health":
		api.Health(w, r)
	case r.Method == http.MethodGet && r.URL.Path == "/1.0/crl":
		api.CRL(w, r)
	case r.Method == http.MethodPost && r.URL.Path == "/1.0/revoke":
		r.Body = http.MaxBytesReader(w, r.Body, 64*1024)
		api.Revoke(w, r)
	case r.Method == http.MethodPost && r.URL.Path == "/1.0/vonk/sign":
		s.sign(w, r)
	default:
		http.NotFound(w, r)
	}
}

func (s *Service) sign(w http.ResponseWriter, r *http.Request) {
	raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 64*1024))
	if err != nil {
		jsonReply(w, 400, map[string]string{"reason_code": "certificate.request_invalid"})
		return
	}
	var body signRequest
	if err := strictJSON(raw, &body); err != nil || body.OTT == "" || (body.Mode != "issue" && body.Mode != "observe") {
		jsonReply(w, 400, map[string]string{"reason_code": "certificate.request_invalid"})
		return
	}
	ctx := provisioner.NewContextWithMethod(r.Context(), provisioner.SignMethod)
	options, err := s.Authority.Authorize(ctx, body.OTT)
	if err != nil {
		jsonReply(w, 401, map[string]string{"reason_code": "certificate.authentication_refused"})
		return
	}
	binding, err := bindingJSON(body.Request)
	if err != nil {
		jsonReply(w, 400, map[string]string{"reason_code": "certificate.request_invalid"})
		return
	}
	block, rest := pem.Decode([]byte(body.CSR))
	if block == nil || block.Type != "CERTIFICATE REQUEST" || len(rest) != 0 {
		jsonReply(w, 400, map[string]string{"reason_code": "certificate.request_invalid"})
		return
	}
	csr, err := x509.ParseCertificateRequest(block.Bytes)
	if err != nil {
		jsonReply(w, 400, map[string]string{"reason_code": "certificate.request_invalid"})
		return
	}
	if err := binding.validate(csr, s.Policy); err != nil {
		jsonReply(w, 400, map[string]string{"reason_code": "certificate.request_invalid"})
		return
	}
	if err := authenticatedBinding(body.OTT, binding, csr); err != nil {
		jsonReply(w, 403, map[string]string{"reason_code": "certificate.binding_refused"})
		return
	}
	// Read the durable effect before mutable source admission. Revoking the old
	// certificate after rotation must not hide its valid replacement.
	receipt, err := s.Journal.Observe(binding)
	if err != nil {
		failReply(w, err)
		return
	}
	var attempt *Attempt
	if receipt != nil && len(receipt.Chain) > 0 {
		attempt = &Attempt{binding, receipt.Epoch, receipt.Owner}
	} else {
		if binding.SourceSerial != nil {
			revoked, err := s.Journal.IsRevoked(*binding.SourceSerial)
			if err != nil {
				failReply(w, err)
				return
			}
			if revoked {
				jsonReply(w, 403, map[string]string{"reason_code": "certificate.source_revoked"})
				return
			}
			source, err := s.Journal.GetCertificate(*binding.SourceSerial)
			if err != nil || source.Subject.CommonName != binding.NodeID || len(source.URIs) != 1 || source.URIs[0].String() != "spiffe://vonk-forge.local/node/"+binding.NodeID || source.CheckSignatureFrom(s.Policy.Issuer) != nil {
				jsonReply(w, 403, map[string]string{"reason_code": "certificate.source_identity_refused"})
				return
			}
		}
		if body.Mode == "observe" {
			if receipt == nil {
				jsonReply(w, 200, absentReply{"absent", binding})
				return
			}
		} else {
			attempt, receipt, err = s.Journal.Claim(binding)
			if err != nil {
				failReply(w, err)
				return
			}
		}
	}
	if attempt == nil {
		jsonReply(w, 202, pendingReply{"pending", binding, "certificate.issuance_in_progress"})
		return
	}
	if receipt == nil || len(receipt.Chain) == 0 {
		before, _ := canonicalTime(binding.NotBefore)
		after, _ := canonicalTime(binding.NotAfter)
		serial, _ := serialNumber(binding.Serial)
		enforcer := provisioner.CertificateEnforcerFunc(func(cert *x509.Certificate) error {
			cert.SerialNumber = serial
			if !cert.NotBefore.Equal(before) || !cert.NotAfter.Equal(after) {
				return errors.New("Authority changed accepted validity")
			}
			return nil
		})
		options = append(options, enforcer)
		// Discard computed return data. Only the fenced durable read below can
		// produce response bytes, including after the Authority's final store.
		_, err = s.Authority.SignWithContext(withAttempt(ctx, *attempt), csr, provisioner.SignOptions{NotBefore: provisioner.NewTimeDuration(before), NotAfter: provisioner.NewTimeDuration(after)}, options...)
		if err != nil {
			failReply(w, err)
			return
		}
	}
	chain, err := s.Journal.ReadCommitted(*attempt)
	if err != nil {
		failReply(w, err)
		return
	}
	if err := s.validateCommitted(binding, csr, chain); err != nil {
		failReply(w, err)
		return
	}
	encoded := make([]string, len(chain))
	for i, certificate := range chain {
		encoded[i] = string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: certificate.Raw}))
	}
	jsonReply(w, 201, issuedReply{"issued", binding, encoded[0], encoded[1], encoded})
}

func (s *Service) validateCommitted(binding Binding, csr *x509.CertificateRequest, chain []*x509.Certificate) error {
	if len(chain) != 2 || !bytes.Equal(chain[1].Raw, s.Policy.Issuer.Raw) {
		return errors.New("committed issuer chain differs from current policy")
	}
	if err := binding.validateLeaf(chain[0], s.Policy, true); err != nil {
		return err
	}
	public, err := x509.MarshalPKIXPublicKey(chain[0].PublicKey)
	if err != nil {
		return err
	}
	expected, err := x509.MarshalPKIXPublicKey(csr.PublicKey)
	if err != nil || !bytes.Equal(public, expected) {
		return errors.New("committed certificate key differs from CSR")
	}
	raw, err := s.Journal.Get(certsDataTable, []byte(binding.Serial))
	if err != nil {
		return err
	}
	var metadata db.CertificateData
	if err := json.Unmarshal(raw, &metadata); err != nil {
		return err
	}
	if metadata.Provisioner == nil || metadata.Provisioner.ID != s.Policy.ProvisionerID || metadata.Provisioner.Name != s.Policy.ProvisionerName || metadata.Provisioner.Type != "JWK" {
		return errors.New("committed certificate metadata differs from current policy")
	}
	return nil
}
