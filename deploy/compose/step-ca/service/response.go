package main

import (
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"errors"
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
