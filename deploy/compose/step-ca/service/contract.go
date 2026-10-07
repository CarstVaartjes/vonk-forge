package main

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"encoding/json"
	"errors"
	"math/big"
	"regexp"
	"time"
)

const certificateLifetime = 30 * 24 * time.Hour

var (
	nodePattern    = regexp.MustCompile(`^spk_[0-9a-f]{32}$`)
	requestPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{43}$`)
	digestPattern  = regexp.MustCompile(`^[0-9a-f]{64}$`)
	serialPattern  = regexp.MustCompile(`^[1-9][0-9]{0,47}$`)
)

// Binding is the exact accepted Controller request. Authentication JWT IDs are
// deliberately separate from this durable effect identity.
type Binding struct {
	RequestID         string  `json:"request_id"`
	NodeID            string  `json:"node_id"`
	CSRSHA256         string  `json:"csr_sha256"`
	Serial            string  `json:"serial"`
	NotBefore         string  `json:"not_before"`
	NotAfter          string  `json:"not_after"`
	IssuerFingerprint string  `json:"issuer_fingerprint"`
	ProvisionerName   string  `json:"provisioner_name"`
	ProvisionerKID    string  `json:"provisioner_kid"`
	PolicySHA256      string  `json:"policy_sha256"`
	Purpose           string  `json:"purpose"`
	SourceSerial      *string `json:"source_serial"`
	Generation        uint64  `json:"generation"`
}

type Policy struct {
	Issuer          *x509.Certificate
	ProvisionerName string
	ProvisionerKID  string
	ProvisionerID   string
	ServerNames     []string
}

func digest(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

func (p Policy) policyDigest() string {
	raw, _ := json.Marshal(map[string]any{
		"certificate_lifetime_seconds": int64(certificateLifetime / time.Second),
		"issuer_fingerprint":           digest(p.Issuer.Raw),
		"profile":                      "vonk-node-client-v1",
		"provisioner_kid":              p.ProvisionerKID,
		"provisioner_name":             p.ProvisionerName,
	})
	return digest(raw)
}

func canonicalTime(value string) (time.Time, error) {
	parsed, err := time.Parse("2006-01-02T15:04:05Z", value)
	if err != nil || parsed.Format("2006-01-02T15:04:05Z") != value {
		return time.Time{}, errors.New("certificate timestamp is not canonical UTC")
	}
	return parsed, nil
}

func serialNumber(value string) (*big.Int, error) {
	serial, ok := new(big.Int).SetString(value, 10)
	if !serialPattern.MatchString(value) || !ok || serial.Sign() <= 0 || serial.BitLen() > 159 {
		return nil, errors.New("certificate serial is invalid")
	}
	return serial, nil
}

func (b Binding) validate(csr *x509.CertificateRequest, p Policy) error {
	if !nodePattern.MatchString(b.NodeID) || !requestPattern.MatchString(b.RequestID) || b.Generation == 0 {
		return errors.New("certificate request identity is invalid")
	}
	if _, err := serialNumber(b.Serial); err != nil {
		return err
	}
	nb, err := canonicalTime(b.NotBefore)
	if err != nil {
		return err
	}
	na, err := canonicalTime(b.NotAfter)
	if err != nil || na.Sub(nb) != certificateLifetime {
		return errors.New("certificate request validity is invalid")
	}
	if !digestPattern.MatchString(b.CSRSHA256) || b.CSRSHA256 != digest(csr.Raw) {
		return errors.New("certificate request CSR binding is invalid")
	}
	if b.IssuerFingerprint != digest(p.Issuer.Raw) || b.ProvisionerName != p.ProvisionerName || b.ProvisionerKID != p.ProvisionerKID || b.PolicySHA256 != p.policyDigest() {
		return errors.New("certificate request provider policy changed")
	}
	if b.Purpose == "rotation" {
		if b.SourceSerial == nil || *b.SourceSerial == b.Serial {
			return errors.New("rotation source identity is invalid")
		}
		if _, err := serialNumber(*b.SourceSerial); err != nil {
			return err
		}
	} else if b.Purpose != "enrollment" || b.SourceSerial != nil {
		return errors.New("certificate request purpose is invalid")
	}
	if err := csr.CheckSignature(); err != nil {
		return errors.New("certificate CSR signature is invalid")
	}
	if _, ok := csr.PublicKey.(ed25519.PublicKey); !ok {
		return errors.New("certificate CSR key must be Ed25519")
	}
	if csr.Subject.CommonName != b.NodeID || len(csr.Subject.Names) != 1 || len(csr.Extensions) != 1 || len(csr.DNSNames) != 0 || len(csr.IPAddresses) != 0 || len(csr.EmailAddresses) != 0 || len(csr.URIs) != 1 || csr.URIs[0].String() != "spiffe://vonk-forge.local/node/"+b.NodeID {
		return errors.New("certificate CSR node identity is invalid")
	}
	return nil
}

func (b Binding) validateLeaf(leaf *x509.Certificate, p Policy, signed bool) error {
	nb, err := canonicalTime(b.NotBefore)
	if err != nil {
		return err
	}
	na, err := canonicalTime(b.NotAfter)
	if err != nil {
		return err
	}
	if leaf.SerialNumber == nil || leaf.SerialNumber.String() != b.Serial || !nodeSubject(leaf.Subject, b.NodeID) || !leaf.NotBefore.Equal(nb) || !leaf.NotAfter.Equal(na) || leaf.IsCA || leaf.KeyUsage != x509.KeyUsageDigitalSignature || len(leaf.ExtKeyUsage) != 1 || leaf.ExtKeyUsage[0] != x509.ExtKeyUsageClientAuth || len(leaf.URIs) != 1 || leaf.URIs[0].String() != "spiffe://vonk-forge.local/node/"+b.NodeID || len(leaf.DNSNames) != 0 || len(leaf.IPAddresses) != 0 || len(leaf.EmailAddresses) != 0 {
		return errors.New("certificate leaf differs from accepted request")
	}
	if signed && (!bytes.Equal(leaf.RawIssuer, p.Issuer.RawSubject) || leaf.CheckSignatureFrom(p.Issuer) != nil) {
		return errors.New("certificate leaf issuer is invalid")
	}
	return nil
}

// Names is populated by ASN.1 decoding, while Authority's generated template
// contains the typed CommonName before DER exists. Validate the same exact
// subject in both representations, retaining checks for decoded unknown OIDs.
func nodeSubject(subject pkix.Name, node string) bool {
	rdns := subject.ToRDNSequence()
	if len(rdns) != 1 || len(rdns[0]) != 1 || rdns[0][0].Type.String() != "2.5.4.3" || rdns[0][0].Value != node {
		return false
	}
	if len(subject.Names) != 0 && (len(subject.Names) != 1 || subject.Names[0].Type.String() != "2.5.4.3" || subject.Names[0].Value != node) {
		return false
	}
	return true
}

func sameBinding(a, b Binding) bool {
	x, _ := json.Marshal(a)
	y, _ := json.Marshal(b)
	return bytes.Equal(x, y)
}

type fault struct {
	code   string
	status int
	cause  error
	detail string
}

func (f *fault) Error() string {
	if f.detail != "" {
		return f.code + ": " + f.detail
	}
	return f.code
}
func refused(code string, status int, cause error) error {
	return &fault{code: code, status: status, cause: cause}
}
