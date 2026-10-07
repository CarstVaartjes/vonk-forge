package main

import (
 "bytes"
 "context"
 "crypto/rand"
 "crypto"
 "crypto/x509"
 "encoding/hex"
 "encoding/json"
 "errors"
 "sync"
 "time"

 "github.com/smallstep/certificates/authority/provisioner"
 casapi "github.com/smallstep/certificates/cas/apiv1"
 "github.com/smallstep/certificates/cas/softcas"
 "github.com/smallstep/certificates/db"
 "github.com/smallstep/nosql"
 "github.com/smallstep/nosql/database"
)

var requestsTable = []byte("vonk_issuance_requests")
var serialsTable = []byte("vonk_issuance_serials")
// These two names are the pinned Smallstep DB format, verified by source.lock.
var certsTable = []byte("x509_certs")
var certsDataTable = []byte("x509_certs_data")

type Receipt struct {
 Binding Binding `json:"binding"`
 Epoch uint64 `json:"epoch"`
 Owner string `json:"owner"`
 LeaseUntil time.Time `json:"lease_until"`
 Chain [][]byte `json:"chain,omitempty"`
}
type Attempt struct { Binding Binding; Epoch uint64; Owner string }
type attemptContextKey struct{}
func withAttempt(ctx context.Context, a Attempt) context.Context { return context.WithValue(ctx, attemptContextKey{}, a) }

// JournalDB serializes only small local DB transactions. Signing, authentication
// and response writing never hold this lock. Badger itself excludes a second
// process from opening this directory. The boot owner fences abandoned workers.
type JournalDB struct {
 *db.DB
 mu sync.Mutex
 owner string
 clock func() time.Time
 lease time.Duration
 BeforeCommit func() error
 AfterCommit func()
}

func newJournalDB(base *db.DB, clock func() time.Time, lease time.Duration) (*JournalDB, error) {
 if base == nil || clock == nil || lease <= 0 || lease > time.Minute { return nil, errors.New("invalid journal configuration") }
 nonce := make([]byte, 32)
 if _, err := rand.Read(nonce); err != nil { return nil, err }
 for _, table := range [][]byte{requestsTable, serialsTable} { if err := base.CreateTable(table); err != nil { return nil, err } }
 return &JournalDB{DB:base, owner:hex.EncodeToString(nonce), clock:clock, lease:lease}, nil
}

func (j *JournalDB) load(b Binding) (*Receipt, error) {
 raw, err := j.Get(requestsTable, []byte(b.RequestID))
 if nosql.IsErrNotFound(err) { return nil, nil }
 if err != nil { return nil, err }
 var r Receipt
 if err := json.Unmarshal(raw, &r); err != nil { return nil, err }
 if !sameBinding(r.Binding, b) || r.Epoch == 0 || r.Owner == "" { return nil, refused("certificate.request_binding_mismatch",409,nil) }
 return &r,nil
}

func (j *JournalDB) Observe(b Binding) (*Receipt,error) {
 j.mu.Lock(); defer j.mu.Unlock()
 return j.load(b)
}

// Claim returns nil when another live attempt owns the exact request.
func (j *JournalDB) Claim(b Binding) (*Attempt,*Receipt,error) {
 j.mu.Lock(); defer j.mu.Unlock()
 r,err := j.load(b); if err != nil { return nil,nil,err }
 now := j.clock().UTC()
 if r != nil && len(r.Chain)>0 { return &Attempt{b,r.Epoch,r.Owner},r,nil }
 if r != nil && r.Owner==j.owner && now.Before(r.LeaseUntil) { return nil,r,nil }
 if r==nil {
  serialOwner,err := j.Get(serialsTable,[]byte(b.Serial))
  if err==nil && string(serialOwner)!=b.RequestID { return nil,nil,refused("certificate.serial_already_reserved",409,nil) }
  if err!=nil && !nosql.IsErrNotFound(err) { return nil,nil,err }
  _,err = j.Get(certsTable,[]byte(b.Serial))
  if err==nil { return nil,nil,refused("certificate.serial_already_issued",409,nil) }
  if !nosql.IsErrNotFound(err) { return nil,nil,err }
  r=&Receipt{Binding:b}
 }
 if r.Epoch==^uint64(0) { return nil,nil,errors.New("journal epoch exhausted") }
 r.Epoch++; r.Owner=j.owner; r.LeaseUntil=now.Add(j.lease)
 raw,err:=json.Marshal(r); if err!=nil { return nil,nil,err }
 tx:=new(database.Tx); tx.Set(requestsTable,[]byte(b.RequestID),raw); tx.Set(serialsTable,[]byte(b.Serial),[]byte(b.RequestID))
 if err:=j.Update(tx); err!=nil { return nil,nil,err }
 return &Attempt{b,r.Epoch,r.Owner},r,nil
}

func matchesAttempt(r *Receipt,a Attempt) bool { return r!=nil && r.Epoch==a.Epoch && r.Owner==a.Owner && sameBinding(r.Binding,a.Binding) }

func (j *JournalDB) Commit(a Attempt, chain []*x509.Certificate, p *casapi.ProvisionerInfo) error {
 j.mu.Lock()
 r,err:=j.load(a.Binding)
 if err!=nil { j.mu.Unlock(); return err }
 if !matchesAttempt(r,a) || !j.clock().Before(r.LeaseUntil) || len(r.Chain)!=0 { j.mu.Unlock(); return refused("certificate.attempt_superseded",409,nil) }
 if len(chain)<2 || p==nil { j.mu.Unlock(); return errors.New("missing signed chain or authenticated provisioner") }
 revoked,err:=j.DB.IsRevoked(a.Binding.Serial)
 if err!=nil || revoked { j.mu.Unlock(); return refused("certificate.issuance_revoked",403,err) }
 if a.Binding.SourceSerial!=nil {
  revoked,err=j.DB.IsRevoked(*a.Binding.SourceSerial)
  if err!=nil || revoked { j.mu.Unlock(); return refused("certificate.rotation_source_revoked",403,err) }
 }
 for _,cert:=range chain { r.Chain=append(r.Chain,cert.Raw) }
 raw,err:=json.Marshal(r); if err!=nil { j.mu.Unlock(); return err }
 metadata,err:=json.Marshal(&db.CertificateData{Provisioner:&db.ProvisionerData{ID:p.ID,Name:p.Name,Type:p.Type}})
 if err!=nil { j.mu.Unlock(); return err }
 if j.BeforeCommit!=nil { if err:=j.BeforeCommit(); err!=nil { j.mu.Unlock(); return err } }
 tx:=new(database.Tx)
 tx.Set(requestsTable,[]byte(a.Binding.RequestID),raw)
 tx.Set(certsTable,[]byte(a.Binding.Serial),chain[0].Raw)
 tx.Set(certsDataTable,[]byte(a.Binding.Serial),metadata)
 err=j.Update(tx)
 j.mu.Unlock()
 if err==nil && j.AfterCommit!=nil { j.AfterCommit() }
 return err
}

// ReadCommitted fences the response independently from the signing/commit fence.
func (j *JournalDB) ReadCommitted(a Attempt) ([]*x509.Certificate,error) {
 j.mu.Lock(); defer j.mu.Unlock()
 r,err:=j.load(a.Binding); if err!=nil { return nil,err }
 if !matchesAttempt(r,a) || len(r.Chain)<2 { return nil,refused("certificate.attempt_superseded",409,nil) }
 revoked,err:=j.DB.IsRevoked(a.Binding.Serial)
 if err!=nil || revoked { return nil,refused("certificate.issuance_revoked",403,err) }
 chain:=make([]*x509.Certificate,0,len(r.Chain))
 for _,der:=range r.Chain { cert,err:=x509.ParseCertificate(der); if err!=nil { return nil,err }; chain=append(chain,cert) }
 stored,err:=j.Get(certsTable,[]byte(a.Binding.Serial))
 if err!=nil || !bytes.Equal(stored,chain[0].Raw) { return nil,errors.New("certificate journal/store integrity failure") }
 return chain,nil
}

// Authority's normal final store must only acknowledge the exact atomic receipt.
func (j *JournalDB) StoreCertificateChain(p provisioner.Interface, chain ...*x509.Certificate) error {
 if len(chain)==0 || p==nil { return errors.New("unbound certificate store") }
 j.mu.Lock(); defer j.mu.Unlock()
 id,err:=j.Get(serialsTable,[]byte(chain[0].SerialNumber.String())); if err!=nil { return err }
 raw,err:=j.Get(requestsTable,id); if err!=nil { return err }
 var r Receipt; if err:=json.Unmarshal(raw,&r); err!=nil { return err }
 if len(r.Chain)==0 || !bytes.Equal(r.Chain[0],chain[0].Raw) || r.Binding.ProvisionerName!=p.GetName() { return errors.New("uncommitted certificate store") }
 return nil
}
func (j *JournalDB) StoreCertificate(cert *x509.Certificate) error { return errors.New("unbound certificate store") }
func (j *JournalDB) Revoke(info *db.RevokedCertificateInfo) error {
 j.mu.Lock(); defer j.mu.Unlock()
 return j.DB.Revoke(info)
}

type JournalCAS struct { Journal *JournalDB; Soft *softcas.SoftCAS; Policy Policy; BeforeSign func(); AfterSign func() }
func (c *JournalCAS) CreateCertificate(req *casapi.CreateCertificateRequest) (*casapi.CreateCertificateResponse,error) { return nil,errors.New("issuance requires exact authenticated context") }
func (c *JournalCAS) RenewCertificate(req *casapi.RenewCertificateRequest) (*casapi.RenewCertificateResponse,error) { return nil,errors.New("rotation requires exact authenticated journal request") }
func (c *JournalCAS) RevokeCertificate(req *casapi.RevokeCertificateRequest) (*casapi.RevokeCertificateResponse,error) { return c.Soft.RevokeCertificate(req) }
func (c *JournalCAS) CreateCRL(req *casapi.CreateCRLRequest) (*casapi.CreateCRLResponse,error) { return c.Soft.CreateCRL(req) }
func (c *JournalCAS) GetSigner() (crypto.Signer,error) { return c.Soft.GetSigner() }
func (c *JournalCAS) Type() casapi.Type { return casapi.SoftCAS }

func (c *JournalCAS) CreateCertificateWithContext(ctx context.Context, req *casapi.CreateCertificateRequest) (*casapi.CreateCertificateResponse,error) {
 a,ok:=ctx.Value(attemptContextKey{}).(Attempt)
 if !ok || req==nil || req.CSR==nil || req.Template==nil || req.Provisioner==nil || req.Provisioner.ID!=c.Policy.ProvisionerID || req.Provisioner.Name!=c.Policy.ProvisionerName { return nil,errors.New("missing exact signing authority") }
 if err:=a.Binding.validate(req.CSR,c.Policy); err!=nil { return nil,err }
 if err:=a.Binding.validateLeaf(req.Template,c.Policy,false); err!=nil { return nil,err }
 if !bytes.Equal(req.CSR.RawSubject,req.Template.RawSubject) && req.Template.RawSubject!=nil { return nil,errors.New("certificate subject changed") }
 if c.BeforeSign!=nil { c.BeforeSign() }
 response,err:=c.Soft.CreateCertificate(req); if err!=nil { return nil,err }
 if c.AfterSign!=nil { c.AfterSign() }
 if err:=a.Binding.validateLeaf(response.Certificate,c.Policy,true); err!=nil { return nil,err }
 key,err:=x509.MarshalPKIXPublicKey(response.Certificate.PublicKey); if err!=nil { return nil,err }
 csrKey,err:=x509.MarshalPKIXPublicKey(req.CSR.PublicKey); if err!=nil || !bytes.Equal(key,csrKey) { return nil,errors.New("certificate key changed") }
 chain:=append([]*x509.Certificate{response.Certificate},response.CertificateChain...)
 if err:=c.Journal.Commit(a,chain,req.Provisioner); err!=nil { return nil,err }
 committed,err:=c.Journal.ReadCommitted(a); if err!=nil { return nil,err }
 return &casapi.CreateCertificateResponse{Certificate:committed[0],CertificateChain:committed[1:]},nil
}

// Only Authority.GetTLSCertificate calls this typed interface. Native client
// issuance never reaches it, including requests setting IsCAServerCert=true.
func (c *JournalCAS) CreateCAServerCertificate(req *casapi.CreateCertificateRequest) (*casapi.CreateCertificateResponse,error) {
 if req==nil || !req.IsCAServerCert || req.CSR==nil || req.Template==nil || req.Provisioner!=nil || req.Lifetime!=24*time.Hour || req.Template.IsCA || req.CSR.CheckSignature()!=nil || len(req.Template.IPAddresses)!=0 || len(req.Template.EmailAddresses)!=0 || len(req.Template.URIs)!=0 || len(req.Template.DNSNames)!=len(c.Policy.ServerNames) { return nil,errors.New("invalid internal CA TLS provenance") }
 for i,name:=range c.Policy.ServerNames { if req.Template.DNSNames[i]!=name { return nil,errors.New("invalid internal CA TLS names") } }
 return c.Soft.CreateCertificate(req)
}
