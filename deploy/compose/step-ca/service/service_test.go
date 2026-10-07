package main

import (
	"bytes"
	"context"
	"crypto"
	"crypto/ecdsa"
	"crypto/ed25519"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/smallstep/certificates/authority"
	"github.com/smallstep/certificates/authority/config"
	"github.com/smallstep/certificates/authority/provisioner"
	"github.com/smallstep/certificates/ca"
	"go.step.sm/crypto/jose"
)

type authorityFixture struct {
	*journalFixture
	auth    *authority.Authority
	handler *Service
	key     *ecdsa.PrivateKey
}

func newAuthorityFixture(t *testing.T) *authorityFixture {
	t.Helper()
	f := &authorityFixture{journalFixture: newJournalFixture(t)}
	var err error
	f.key, err = ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	public := &jose.JSONWebKey{Key: &f.key.PublicKey, Algorithm: "ES256", Use: "sig"}
	thumbprint, err := public.Thumbprint(crypto.SHA256)
	if err != nil {
		t.Fatal(err)
	}
	public.KeyID = base64.RawURLEncoding.EncodeToString(thumbprint)
	f.c.Policy.ProvisionerKID = public.KeyID
	f.binding.ProvisionerKID = public.KeyID
	f.binding.PolicySHA256 = f.c.Policy.policyDigest()
	disabled := true
	p := &provisioner.JWK{Type: "JWK", Name: f.c.Policy.ProvisionerName, Key: public, Claims: &provisioner.Claims{MinTLSDur: &provisioner.Duration{Duration: certificateLifetime}, MaxTLSDur: &provisioner.Duration{Duration: certificateLifetime}, DefaultTLSDur: &provisioner.Duration{Duration: certificateLifetime}, DisableRenewal: &disabled, DisableSmallstepExtensions: &disabled}, Options: &provisioner.Options{X509: &provisioner.X509Options{Template: nodeTemplate}}}
	f.c.Policy.ProvisionerID = p.GetID()
	cfg := &config.Config{Address: ":9000", DNSNames: []string{"step-ca"}, AuthorityConfig: &config.AuthConfig{Provisioners: provisioner.List{p}}, CRL: &config.CRLConfig{Enabled: true, GenerateOnRevoke: true}}
	f.auth, err = authority.NewEmbedded(authority.WithConfig(cfg), authority.WithDatabase(f.j), authority.WithX509CAService(f.c), authority.WithX509RootCerts(f.c.Policy.Issuer), authority.WithX509IntermediateCerts(f.c.Policy.Issuer), authority.WithQuietInit())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = f.auth.Shutdown() })
	f.handler = NewService(f.auth, f.c, f.j, f.c.Policy)
	return f
}

func (f *authorityFixture) token(t *testing.T, binding Binding, change func(map[string]any)) string {
	t.Helper()
	nonce := make([]byte, 32)
	if _, err := rand.Read(nonce); err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(f.csr.Raw)
	now := time.Now().UTC()
	claims := map[string]any{"iss": f.c.Policy.ProvisionerName, "sub": binding.NodeID, "aud": "https://step-ca/1.0/sign", "iat": now.Unix(), "nbf": now.Add(-time.Second).Unix(), "exp": now.Add(time.Minute).Unix(), "jti": base64.RawURLEncoding.EncodeToString(nonce), "sans": []string{"spiffe://vonk-forge.local/node/" + binding.NodeID}, "vonk": binding, "cnf": map[string]string{"x5rt#S256": base64.RawURLEncoding.EncodeToString(sum[:])}}
	if change != nil {
		change(claims)
	}
	signer, err := jose.NewSigner(jose.SigningKey{Algorithm: jose.ES256, Key: f.key}, (&jose.SignerOptions{}).WithType("JWT").WithHeader("kid", f.c.Policy.ProvisionerKID))
	if err != nil {
		t.Fatal(err)
	}
	token, err := jose.Signed(signer).Claims(claims).CompactSerialize()
	if err != nil {
		t.Fatal(err)
	}
	return token
}
func (f *authorityFixture) call(t *testing.T, binding Binding, token, mode string) *httptest.ResponseRecorder {
	t.Helper()
	raw, err := json.Marshal(map[string]any{"csr": string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE REQUEST", Bytes: f.csr.Raw})), "ott": token, "request": binding, "mode": mode})
	if err != nil {
		t.Fatal(err)
	}
	response := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "https://step-ca/1.0/vonk/sign", bytes.NewReader(raw))
	f.handler.ServeHTTP(response, request)
	return response
}
func issuedLeaf(t *testing.T, response *httptest.ResponseRecorder) *x509.Certificate {
	t.Helper()
	if response.Code != 201 {
		t.Fatalf("expected issued response, got %d: %s", response.Code, response.Body.String())
	}
	var reply issuedReply
	if err := json.Unmarshal(response.Body.Bytes(), &reply); err != nil {
		t.Fatal(err)
	}
	block, _ := pem.Decode([]byte(reply.CRT))
	if block == nil {
		t.Fatal("missing committed leaf")
	}
	leaf, err := x509.ParseCertificate(block.Bytes)
	if err != nil {
		t.Fatal(err)
	}
	return leaf
}

func TestAuthorityHTTPFreshAuthenticationAdoptsExactCommittedDER(t *testing.T) {
	f := newAuthorityFixture(t)
	firstToken := f.token(t, f.binding, nil)
	first := issuedLeaf(t, f.call(t, f.binding, firstToken, "issue"))
	if err := first.CheckSignatureFrom(f.c.Policy.Issuer); err != nil {
		t.Fatal(err)
	}
	if first.SerialNumber.String() != f.binding.Serial {
		t.Fatal("Authority ignored reserved serial")
	}
	replay := f.call(t, f.binding, firstToken, "observe")
	if replay.Code != 401 || strings.Contains(replay.Body.String(), "BEGIN CERTIFICATE") {
		t.Fatalf("reused JWT bypassed normal authorization: %s", replay.Body.String())
	}
	signerCalled := false
	f.c.BeforeSign = func() { signerCalled = true }
	resumed := issuedLeaf(t, f.call(t, f.binding, f.token(t, f.binding, nil), "observe"))
	if signerCalled || !bytes.Equal(first.Raw, resumed.Raw) {
		t.Fatal("fresh observer recomputed committed effect")
	}
}

func TestAuthorityHTTPRejectsUnauthenticatedAndChangedBindings(t *testing.T) {
	cases := map[string]func(map[string]any){"audience": func(c map[string]any) { c["aud"] = "https://other.invalid/1.0/sign" }, "expired": func(c map[string]any) { c["exp"] = time.Now().Add(-10 * time.Minute).Unix() }, "confirmation": func(c map[string]any) { c["cnf"] = map[string]string{"x5rt#S256": "wrong"} }, "stable_jti": func(c map[string]any) { c["jti"] = c["vonk"].(Binding).RequestID }, "changed_serial": func(c map[string]any) { b := c["vonk"].(Binding); b.Serial = "987"; c["vonk"] = b }}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			f := newAuthorityFixture(t)
			response := f.call(t, f.binding, f.token(t, f.binding, mutate), "issue")
			if response.Code < 400 || strings.Contains(response.Body.String(), "BEGIN CERTIFICATE") {
				t.Fatalf("invalid request leaked certificate: %d %s", response.Code, response.Body.String())
			}
			receipt, err := f.j.Observe(f.binding)
			if err != nil || receipt != nil {
				t.Fatalf("refused authentication reserved issuance: %v", err)
			}
		})
	}
	t.Run("signature", func(t *testing.T) {
		f := newAuthorityFixture(t)
		token := f.token(t, f.binding, nil)
		parts := strings.Split(token, ".")
		signature, err := base64.RawURLEncoding.DecodeString(parts[2])
		if err != nil {
			t.Fatal(err)
		}
		signature[0] ^= 1
		parts[2] = base64.RawURLEncoding.EncodeToString(signature)
		response := f.call(t, f.binding, strings.Join(parts, "."), "issue")
		if response.Code != 401 {
			t.Fatalf("wrong JWT signature accepted: %d", response.Code)
		}
	})
}

func TestAuthorityHTTPReplaysCannotChangeCSRSerialOrValidity(t *testing.T) {
	f := newAuthorityFixture(t)
	original := issuedLeaf(t, f.call(t, f.binding, f.token(t, f.binding, nil), "issue"))
	variants := map[string]func(*Binding){"serial": func(b *Binding) { b.Serial = "123456789" }, "validity": func(b *Binding) {
		before, _ := canonicalTime(b.NotBefore)
		after, _ := canonicalTime(b.NotAfter)
		b.NotBefore = before.Add(time.Second).Format("2006-01-02T15:04:05Z")
		b.NotAfter = after.Add(time.Second).Format("2006-01-02T15:04:05Z")
	}, "generation": func(b *Binding) { b.Generation++ }}
	for name, mutate := range variants {
		t.Run(name, func(t *testing.T) {
			binding := f.binding
			mutate(&binding)
			response := f.call(t, binding, f.token(t, binding, nil), "issue")
			if response.Code < 400 || strings.Contains(response.Body.String(), "BEGIN CERTIFICATE") {
				t.Fatalf("changed %s adopted another identity: %s", name, response.Body.String())
			}
		})
	}
	_, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	der, err := x509.CreateCertificateRequest(rand.Reader, &x509.CertificateRequest{Subject: f.csr.Subject, URIs: f.csr.URIs}, key)
	if err != nil {
		t.Fatal(err)
	}
	changed, err := x509.ParseCertificateRequest(der)
	if err != nil {
		t.Fatal(err)
	}
	previous := f.csr
	f.csr = changed
	binding := f.binding
	binding.CSRSHA256 = digest(changed.Raw)
	response := f.call(t, binding, f.token(t, binding, nil), "issue")
	if response.Code < 400 || strings.Contains(response.Body.String(), "BEGIN CERTIFICATE") {
		t.Fatal("changed CSR reissued committed request")
	}
	f.csr = previous
	recovered := issuedLeaf(t, f.call(t, f.binding, f.token(t, f.binding, nil), "observe"))
	if !bytes.Equal(original.Raw, recovered.Raw) {
		t.Fatal("refused replays damaged original valid certificate")
	}
}

func TestCommittedRotationRemainsObservableAfterSourceRevocation(t *testing.T) {
	f := newAuthorityFixture(t)
	initial := issuedLeaf(t, f.call(t, f.binding, f.token(t, f.binding, nil), "issue"))
	source := initial.SerialNumber.String()
	_, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	der, err := x509.CreateCertificateRequest(rand.Reader, &x509.CertificateRequest{Subject: f.csr.Subject, URIs: f.csr.URIs}, key)
	if err != nil {
		t.Fatal(err)
	}
	f.csr, err = x509.ParseCertificateRequest(der)
	if err != nil {
		t.Fatal(err)
	}
	rotated := f.binding
	rotated.RequestID = strings.Repeat("r", 43)
	rotated.Serial = "234567891"
	rotated.CSRSHA256 = digest(der)
	rotated.Purpose = "rotation"
	rotated.SourceSerial = &source
	rotated.Generation++
	replacement := issuedLeaf(t, f.call(t, rotated, f.token(t, rotated, nil), "issue"))
	token := f.token(t, rotated, func(claims map[string]any) {
		claims["sub"] = source
		claims["aud"] = "https://step-ca/1.0/revoke"
		delete(claims, "vonk")
		delete(claims, "cnf")
		delete(claims, "sans")
	})
	raw, _ := json.Marshal(map[string]any{"serial": source, "ott": token, "passive": true, "reasonCode": 0})
	response := httptest.NewRecorder()
	f.handler.ServeHTTP(response, httptest.NewRequest(http.MethodPost, "https://step-ca/1.0/revoke", bytes.NewReader(raw)))
	if response.Code != 200 {
		t.Fatalf("source revoke failed: %s", response.Body.String())
	}
	signerCalled := false
	f.c.BeforeSign = func() { signerCalled = true }
	observed := issuedLeaf(t, f.call(t, rotated, f.token(t, rotated, nil), "observe"))
	replayed := issuedLeaf(t, f.call(t, rotated, f.token(t, rotated, nil), "issue"))
	if signerCalled || !bytes.Equal(replacement.Raw, observed.Raw) || !bytes.Equal(replacement.Raw, replayed.Raw) {
		t.Fatal("source revocation hid or recomputed committed valid replacement")
	}
	pending := rotated
	pending.RequestID = strings.Repeat("p", 43)
	pending.Serial = "234567892"
	refused := f.call(t, pending, f.token(t, pending, nil), "issue")
	if refused.Code != 403 || strings.Contains(refused.Body.String(), "BEGIN CERTIFICATE") {
		t.Fatal("revoked source admitted new issuance")
	}
}

func TestAuthorityNativeRevocationRemainsEffectiveForJournalObservation(t *testing.T) {
	f := newAuthorityFixture(t)
	leaf := issuedLeaf(t, f.call(t, f.binding, f.token(t, f.binding, nil), "issue"))
	token := f.token(t, f.binding, func(claims map[string]any) {
		claims["sub"] = leaf.SerialNumber.String()
		claims["aud"] = "https://step-ca/1.0/revoke"
		delete(claims, "vonk")
		delete(claims, "cnf")
		delete(claims, "sans")
	})
	raw, _ := json.Marshal(map[string]any{"serial": leaf.SerialNumber.String(), "ott": token, "passive": true, "reasonCode": 0})
	response := httptest.NewRecorder()
	f.handler.ServeHTTP(response, httptest.NewRequest(http.MethodPost, "https://step-ca/1.0/revoke", bytes.NewReader(raw)))
	if response.Code != 200 {
		t.Fatalf("existing native revoke failed: %s", response.Body.String())
	}
	observer := f.call(t, f.binding, f.token(t, f.binding, nil), "observe")
	if observer.Code != 403 || strings.Contains(observer.Body.String(), "BEGIN CERTIFICATE") {
		t.Fatal("revoked committed certificate remained adoptable")
	}
	crl := httptest.NewRecorder()
	f.handler.ServeHTTP(crl, httptest.NewRequest(http.MethodGet, "/1.0/crl?pem", nil))
	block, _ := pem.Decode(crl.Body.Bytes())
	if crl.Code != 200 || block == nil {
		t.Fatal("native revocation failed to regenerate CRL")
	}
	list, err := x509.ParseRevocationList(block.Bytes)
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, entry := range list.RevokedCertificateEntries {
		if entry.SerialNumber.Cmp(leaf.SerialNumber) == 0 {
			found = true
		}
	}
	if !found {
		t.Fatal("fresh CRL omitted journal certificate revocation")
	}
}

func TestRuntimeTLSRetainsAuthenticatedClientSelfRevocation(t *testing.T) {
	f := newAuthorityFixture(t)
	_, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	der, err := x509.CreateCertificateRequest(rand.Reader, &x509.CertificateRequest{Subject: f.csr.Subject, URIs: f.csr.URIs}, key)
	if err != nil {
		t.Fatal(err)
	}
	f.csr, err = x509.ParseCertificateRequest(der)
	if err != nil {
		t.Fatal(err)
	}
	f.binding.CSRSHA256 = digest(der)
	response := f.call(t, f.binding, f.token(t, f.binding, nil), "issue")
	leaf := issuedLeaf(t, response)
	var issued issuedReply
	if err := json.Unmarshal(response.Body.Bytes(), &issued); err != nil {
		t.Fatal(err)
	}
	private, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	clientCertificate, err := tls.X509KeyPair([]byte(strings.Join(issued.Chain, "")), pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: private}))
	if err != nil {
		t.Fatal(err)
	}
	initial, err := f.auth.GetTLSCertificate()
	if err != nil {
		t.Fatal(err)
	}
	renewer, err := ca.NewTLSRenewer(initial, f.auth.GetTLSCertificate)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewUnstartedServer(f.handler)
	server.TLS = serverTLS(f.auth, &config.Config{}, renewer)
	server.StartTLS()
	defer server.Close()
	pool := x509.NewCertPool()
	pool.AddCert(f.c.Policy.Issuer)
	client := &http.Client{Transport: &http.Transport{TLSClientConfig: &tls.Config{MinVersion: tls.VersionTLS12, ServerName: "step-ca", RootCAs: pool, Certificates: []tls.Certificate{clientCertificate}}}, Timeout: 5 * time.Second}
	defer client.CloseIdleConnections()
	raw, _ := json.Marshal(map[string]any{"serial": leaf.SerialNumber.String(), "passive": true, "reasonCode": 0})
	revoked, err := client.Post(server.URL+"/1.0/revoke", "application/json", bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	defer revoked.Body.Close()
	if revoked.StatusCode != 200 {
		t.Fatalf("authenticated client self-revocation failed: %d", revoked.StatusCode)
	}
	if observed := f.call(t, f.binding, f.token(t, f.binding, nil), "observe"); observed.Code != 403 {
		t.Fatal("mTLS revocation did not fence journal replay")
	}
}

func TestAuthorityHTTPStorageFaultAndLostHTTPRecoverWithoutNewIdentity(t *testing.T) {
	f := newAuthorityFixture(t)
	f.j.BeforeCommit = func() error { return errors.New("injected real commit boundary failure") }
	refused := f.call(t, f.binding, f.token(t, f.binding, nil), "issue")
	if refused.Code < 400 || strings.Contains(refused.Body.String(), "BEGIN CERTIFICATE") {
		t.Fatal("failed CA commit leaked local signature")
	}
	f.j.BeforeCommit = nil
	f.now.Add(11)
	// Close the actual HTTP server's connections after durable commit, before
	// the handler can return its DER. A fresh HTTP observation must recover it.
	server := httptest.NewServer(f.handler)
	defer server.Close()
	f.j.AfterCommit = server.CloseClientConnections
	raw, _ := json.Marshal(map[string]any{"csr": string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE REQUEST", Bytes: f.csr.Raw})), "ott": f.token(t, f.binding, nil), "request": f.binding, "mode": "issue"})
	response, err := server.Client().Post(server.URL+"/1.0/vonk/sign", "application/json", bytes.NewReader(raw))
	if err == nil {
		_ = response.Body.Close()
		t.Fatal("lost-response injection unexpectedly delivered HTTP reply")
	}
	receipt, err := f.j.Observe(f.binding)
	if err != nil || receipt == nil || len(receipt.Chain) == 0 {
		t.Fatalf("lost HTTP did not leave durable commit: %v", err)
	}
	f.c.BeforeSign = func() { t.Error("lost HTTP recovery called signer again") }
	recovered := issuedLeaf(t, f.call(t, f.binding, f.token(t, f.binding, nil), "observe"))
	if !bytes.Equal(receipt.Chain[0], recovered.Raw) {
		t.Fatal("lost HTTP recovery changed DER")
	}
}

func TestAuthorityInternalTLSRenewsAndClientRoutesRemainFenced(t *testing.T) {
	f := newAuthorityFixture(t)
	initial, err := f.auth.GetTLSCertificate()
	if err != nil {
		t.Fatal(err)
	}
	renewed := make(chan struct{}, 1)
	renewer, err := ca.NewTLSRenewer(initial, func() (*tls.Certificate, error) {
		certificate, err := f.auth.GetTLSCertificate()
		if err == nil {
			select {
			case renewed <- struct{}{}:
			default:
			}
		}
		return certificate, err
	}, ca.WithRenewBefore(24*time.Hour-time.Second), ca.WithRenewJitter(time.Nanosecond))
	if err != nil {
		t.Fatal(err)
	}
	renewer.Run()
	defer renewer.Stop()
	select {
	case <-renewed:
	case <-time.After(5 * time.Second):
		t.Fatal("automatic internal CA TLS renewal failed")
	}
	for _, path := range []string{"/1.0/renew", "/1.0/rekey", "/sign", "/1.0/sign"} {
		response := httptest.NewRecorder()
		f.handler.ServeHTTP(response, httptest.NewRequest(http.MethodPost, path, strings.NewReader(`{}`)))
		if response.Code != 404 {
			t.Fatalf("native issuance route remains: %s", path)
		}
	}
	if response, err := f.c.CreateCertificate(f.request()); err == nil || response != nil {
		t.Fatal("old CAS callback admitted client certificate")
	}
	forged := f.request()
	forged.IsCAServerCert = true
	if response, err := f.c.CreateCertificate(forged); err == nil || response != nil {
		t.Fatal("client boolean invoked internal TLS callback")
	}
	if response, err := f.c.CreateCertificateWithContext(context.Background(), f.request()); err == nil || response != nil {
		t.Fatal("erased attempt context admitted certificate")
	}
	leaf := issuedLeaf(t, f.call(t, f.binding, f.token(t, f.binding, nil), "issue"))
	if _, err := f.auth.Renew(leaf); err == nil {
		t.Fatal("native Authority renewal ignored disabled policy")
	}
	crl := httptest.NewRecorder()
	f.handler.ServeHTTP(crl, httptest.NewRequest(http.MethodGet, "/1.0/crl?pem", nil))
	if crl.Code != 200 {
		t.Fatalf("existing CRL path failed: %s", crl.Body.String())
	}
}

func TestHTTPRequiredNullableAndDuplicateBindingFieldsFailClosed(t *testing.T) {
	f := newAuthorityFixture(t)
	raw, _ := json.Marshal(f.binding)
	absent := bytes.Replace(raw, []byte(`,"source_serial":null`), nil, 1)
	if _, err := bindingJSON(absent); err == nil {
		t.Fatal("omitted required source_serial became null")
	}
	duplicate := append([]byte(`{"source_serial":null,`), raw[1:]...)
	if _, err := bindingJSON(duplicate); err == nil {
		t.Fatal("duplicate binding fields accepted")
	}
	var parsed any
	if err := strictJSON([]byte(`{"outer":{"a":1,"a":2}}`), &parsed); err == nil {
		t.Fatal("nested duplicate JSON keys accepted")
	}
}

// The ordinary Go lane checks actual Authority/Service refusal bodies. The
// hosted connected lane additionally runs the locked Python provider against
// this real HTTPS server; no mocked producer or Python transport is involved.
func TestAuthorityHTTPCanonicalRefusalsReachNativeProvider(t *testing.T) {
	for _, name := range []string{"authentication", "revoked", "capacity"} {
		t.Run(name, func(t *testing.T) {
			f := newAuthorityFixture(t)
			code, status, mode := "certificate.authentication_refused", 401, "issue"
			token := "invalid"
			if name == "revoked" {
				leaf := issuedLeaf(t, f.call(t, f.binding, f.token(t, f.binding, nil), "issue"))
				revokeToken := f.token(t, f.binding, func(claims map[string]any) {
					claims["aud"] = "https://step-ca/1.0/revoke"
					claims["sub"] = leaf.SerialNumber.String()
					delete(claims, "sans")
				})
				raw, _ := json.Marshal(map[string]any{"serial": leaf.SerialNumber.String(), "ott": revokeToken, "passive": true, "reasonCode": 0})
				response := httptest.NewRecorder()
				f.handler.ServeHTTP(response, httptest.NewRequest(http.MethodPost, "https://step-ca/1.0/revoke", bytes.NewReader(raw)))
				if response.Code != 200 {
					t.Fatalf("actual native revocation failed: %d", response.Code)
				}
				code, status, mode = "certificate.issuance_revoked", 403, "observe"
				token = f.token(t, f.binding, nil)
			}
			if name == "capacity" {
				issuerTemplate := *f.c.Policy.Issuer
				issuerTemplate.RawSubject = nil
				issuerTemplate.Subject = pkix.Name{CommonName: strings.Repeat("i", 24*1024)}
				der, err := x509.CreateCertificate(rand.Reader, &issuerTemplate, &issuerTemplate, issuerTemplate.PublicKey, f.c.Soft.Signer)
				if err != nil {
					t.Fatal(err)
				}
				issuer, err := x509.ParseCertificate(der)
				if err != nil {
					t.Fatal(err)
				}
				f.c.Policy.Issuer = issuer
				f.c.Soft.CertificateChain = []*x509.Certificate{issuer}
				f.binding.IssuerFingerprint = digest(issuer.Raw)
				f.binding.PolicySHA256 = f.c.Policy.policyDigest()
				f.handler.Policy = f.c.Policy
				code, status = "certificate.response_unrepresentable", 422
				token = f.token(t, f.binding, nil)
			}
			signerEntered := false
			f.c.BeforeSign = func() { signerEntered = true }
			response := f.call(t, f.binding, token, mode)
			var reply refusalReply
			if err := strictJSON(response.Body.Bytes(), &reply); err != nil || response.Code != status || reply.Reason != code || reply.Detail == "" || len(reply.Detail) > 512 {
				t.Fatalf("actual refusal violated canonical contract: %d %s", response.Code, response.Body.String())
			}
			if name == "capacity" {
				var observed, limit int
				if _, err := fmt.Sscanf(reply.Detail, "Certificate response requires %d bytes; limit is %d bytes.", &observed, &limit); err != nil || observed <= limit || limit != maxIssuedResponseBytes {
					t.Fatalf("capacity refusal lost measured byte cause: %s", reply.Detail)
				}
				// The first refused claim remains truthful pending. Expiry permits
				// the same immutable request to try again, never a new identity.
				f.now.Add(11)
			}
			if python := os.Getenv("VONK_CA_REFUSAL_PYTHON"); python != "" {
				server := httptest.NewTLSServer(f.handler)
				defer server.Close()
				if name != "authentication" {
					token = f.token(t, f.binding, nil)
				}
				payload, err := json.Marshal(map[string]any{
					"origin":          server.URL,
					"tls_certificate": string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: server.Certificate().Raw})),
					"body":            signRequest{string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE REQUEST", Bytes: f.csr.Raw})), token, mustBindingJSON(t, f.binding), mode},
					"expected":        reply,
				})
				if err != nil {
					t.Fatal(err)
				}
				root, err := filepath.Abs("../../../..")
				if err != nil {
					t.Fatal(err)
				}
				childContext, cancel := context.WithTimeout(context.Background(), 60*time.Second)
				defer cancel()
				command := exec.CommandContext(childContext, python, "-m", "tests.ca_refusal_consumer")
				command.Dir = filepath.Join(root, "control")
				command.Stdin = bytes.NewReader(payload)
				if output, err := command.CombinedOutput(); err != nil {
					t.Fatalf("native Python provider lost real HTTP refusal: %v %s", err, output)
				}
			}
			if signerEntered {
				t.Fatal("refused request invoked private signer")
			}
			if name == "capacity" {
				receipt, err := f.j.Observe(f.binding)
				if err != nil || receipt == nil || len(receipt.Chain) != 0 {
					t.Fatal("capacity refusal published certificate receipt")
				}
			}
		})
	}
}

func mustBindingJSON(t *testing.T, binding Binding) json.RawMessage {
	t.Helper()
	raw, err := json.Marshal(binding)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}
