package main

import (
	"crypto"
	"crypto/ed25519"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/asn1"
	"io"
	"math/big"
	"net/url"
	"strings"
	"time"

	"encoding/json"
	"encoding/pem"
	"errors"
	"go.step.sm/crypto/x509util"
)

// This is the private issuance wire budget, including the exact binding and
// duplicated PEM fields. Agent envelopes have their own canonical full budget.
const maxIssuedResponseBytes = 64 * 1024

func makeIssuedReply(binding Binding, chain []*x509.Certificate) (issuedReply, error) {
	if len(chain) != 2 {
		return issuedReply{}, errors.New("issuance requires the exact leaf and issuer chain")
	}
	encoded := make([]string, len(chain))
	for i, certificate := range chain {
		encoded[i] = string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: certificate.Raw}))
	}
	reply := issuedReply{"issued", binding, encoded[0], encoded[1], encoded}
	raw, err := json.Marshal(reply)
	if err != nil {
		return issuedReply{}, err
	}
	// jsonReply uses Encoder.Encode, which appends one newline.
	if len(raw)+1 > maxIssuedResponseBytes {
		return issuedReply{}, refused("certificate.response_unrepresentable", 422, nil)
	}
	return reply, nil
}

// captureSigner has only the issuer PUBLIC key. The sentinel aborts x509's
// signing call after it has built the exact TBS, before any private operation.
var errCaptureTBS = errors.New("public certificate sizing only")

type captureSigner struct {
	public crypto.PublicKey
	tbs    []byte
}

func (s *captureSigner) Public() crypto.PublicKey { return s.public }
func (s *captureSigner) Sign(_ io.Reader, message []byte, opts crypto.SignerOpts) ([]byte, error) {
	if opts.HashFunc() != 0 {
		return nil, errors.New("sizing requires Ed25519")
	}
	s.tbs = append([]byte(nil), message...)
	return nil, errCaptureTBS
}

func projectedCertificate(template, issuer *x509.Certificate, public crypto.PublicKey) (*x509.Certificate, error) {
	if _, ok := issuer.PublicKey.(ed25519.PublicKey); !ok {
		return nil, errors.New("managed issuance requires Ed25519 issuer")
	}
	if template.SignatureAlgorithm != 0 && template.SignatureAlgorithm != x509.PureEd25519 {
		return nil, errors.New("managed issuance signature algorithm changed")
	}
	copy := *template
	copy.Issuer = issuer.Subject
	copy.SignatureAlgorithm = x509.PureEd25519
	signer := &captureSigner{public: issuer.PublicKey}
	_, err := x509util.CreateCertificate(&copy, issuer, public, signer)
	if !errors.Is(err, errCaptureTBS) || len(signer.tbs) == 0 {
		return nil, errors.New("public certificate sizing failed")
	}
	// This invalid signature is solely a size projection. It never reaches the
	// store, Authority, HTTP response, or any certificate authorization reader.
	raw, err := asn1.Marshal(struct {
		TBS       asn1.RawValue
		Algorithm pkix.AlgorithmIdentifier
		Signature asn1.BitString
	}{asn1.RawValue{FullBytes: signer.tbs}, pkix.AlgorithmIdentifier{Algorithm: asn1.ObjectIdentifier{1, 3, 101, 112}}, asn1.BitString{Bytes: make([]byte, ed25519.SignatureSize), BitLength: ed25519.SignatureSize * 8}})
	if err != nil {
		return nil, err
	}
	return x509.ParseCertificate(raw)
}

func validateProjectedResponse(binding Binding, template, issuer *x509.Certificate, public crypto.PublicKey) error {
	leaf, err := projectedCertificate(template, issuer, public)
	if err != nil {
		return err
	}
	_, err = makeIssuedReply(binding, []*x509.Certificate{leaf, issuer})
	return err
}

// All variable profile fields use their canonical maximum encoded lengths.
// Both validity dates use GeneralizedTime, the longer supported X.509 form.
func validateStartupResponseBudget(policy Policy) error {
	node := "spk_" + strings.Repeat("f", 32)
	uri, err := url.Parse("spiffe://vonk-forge.local/node/" + node)
	if err != nil {
		return err
	}
	serial := new(big.Int).Sub(new(big.Int).Lsh(big.NewInt(1), 159), big.NewInt(1))
	source := new(big.Int).Sub(serial, big.NewInt(1)).String()
	nb := time.Date(2051, 1, 1, 0, 0, 0, 0, time.UTC)
	na := nb.Add(certificateLifetime)
	public := ed25519.PublicKey(make([]byte, ed25519.PublicKeySize))
	template := &x509.Certificate{SerialNumber: serial, Subject: pkix.Name{CommonName: node}, PublicKey: public, NotBefore: nb, NotAfter: na, KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}, URIs: []*url.URL{uri}}
	binding := Binding{RequestID: strings.Repeat("a", 43), NodeID: node, CSRSHA256: strings.Repeat("f", 64), Serial: serial.String(), NotBefore: nb.Format("2006-01-02T15:04:05Z"), NotAfter: na.Format("2006-01-02T15:04:05Z"), IssuerFingerprint: digest(policy.Issuer.Raw), ProvisionerName: policy.ProvisionerName, ProvisionerKID: policy.ProvisionerKID, PolicySHA256: policy.policyDigest(), Purpose: "rotation", SourceSerial: &source, Generation: ^uint64(0)}
	return validateProjectedResponse(binding, template, policy.Issuer, public)
}
