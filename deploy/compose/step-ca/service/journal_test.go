package main

import (
 "bytes"
 "context"
 "crypto/ed25519"
 "crypto/rand"
 "crypto/x509"
 "crypto/x509/pkix"
 "errors"
 "math/big"
 "net/url"
 "strings"
 "sync/atomic"
 "testing"
 "time"

 casapi "github.com/smallstep/certificates/cas/apiv1"
 "github.com/smallstep/certificates/cas/softcas"
 "github.com/smallstep/certificates/db"
 "github.com/smallstep/nosql"
)

type journalFixture struct { j *JournalDB; c *JournalCAS; csr *x509.CertificateRequest; binding Binding; now atomic.Int64; path string }

func newJournalFixture(t *testing.T) *journalFixture {
 t.Helper()
 f:=&journalFixture{path:t.TempDir()}; f.now.Store(time.Now().UTC().Truncate(time.Second).Unix())
 clock:=func()time.Time{return time.Unix(f.now.Load(),0).UTC()}
 authdb,err:=db.New(&db.Config{Type:"badgerv2",DataSource:f.path}); if err!=nil {t.Fatal(err)}
 base,ok:=authdb.(*db.DB); if !ok {t.Fatal("configured Badger is not concrete DB")}
 t.Cleanup(func(){_ = authdb.Shutdown()})
 f.j,err=newJournalDB(base,clock,10*time.Second); if err!=nil {t.Fatal(err)}
 issuerPub,issuerKey,err:=ed25519.GenerateKey(rand.Reader); if err!=nil {t.Fatal(err)}
 issuerTemplate:=&x509.Certificate{SerialNumber:big.NewInt(1),Subject:pkix.Name{CommonName:"journal-test-issuer"},NotBefore:clock().Add(-time.Hour),NotAfter:clock().Add(365*24*time.Hour),IsCA:true,BasicConstraintsValid:true,KeyUsage:x509.KeyUsageCertSign|x509.KeyUsageCRLSign}
 der,err:=x509.CreateCertificate(rand.Reader,issuerTemplate,issuerTemplate,issuerPub,issuerKey); if err!=nil {t.Fatal(err)}
 issuer,err:=x509.ParseCertificate(der); if err!=nil {t.Fatal(err)}
 soft,err:=softcas.New(context.Background(),casapi.Options{CertificateChain:[]*x509.Certificate{issuer},Signer:issuerKey}); if err!=nil {t.Fatal(err)}
 policy:=Policy{Issuer:issuer,ProvisionerName:"vonk-forge-agent",ProvisionerKID:"test-kid",ProvisionerID:"test-provider",ServerNames:[]string{"step-ca"}}
 f.c=&JournalCAS{Journal:f.j,Soft:soft,Policy:policy}
 _,key,err:=ed25519.GenerateKey(rand.Reader); if err!=nil {t.Fatal(err)}
 node:="spk_"+strings.Repeat("a",32)
 uri,err:=url.Parse("spiffe://vonk-forge.local/node/"+node); if err!=nil {t.Fatal(err)}
 csrDER,err:=x509.CreateCertificateRequest(rand.Reader,&x509.CertificateRequest{Subject:pkix.Name{CommonName:node},URIs:[]*url.URL{uri}},key); if err!=nil {t.Fatal(err)}
 f.csr,err=x509.ParseCertificateRequest(csrDER); if err!=nil {t.Fatal(err)}
 f.binding=Binding{RequestID:strings.Repeat("a",43),NodeID:node,CSRSHA256:digest(csrDER),Serial:"17",NotBefore:clock().Format("2006-01-02T15:04:05Z"),NotAfter:clock().Add(certificateLifetime).Format("2006-01-02T15:04:05Z"),IssuerFingerprint:digest(issuer.Raw),ProvisionerName:policy.ProvisionerName,ProvisionerKID:policy.ProvisionerKID,PolicySHA256:policy.policyDigest(),Purpose:"enrollment",Generation:1}
 if err:=f.binding.validate(f.csr,policy); err!=nil {t.Fatal(err)}
 return f
}

func (f *journalFixture) request() *casapi.CreateCertificateRequest {
 serial,_:=serialNumber(f.binding.Serial); nb,_:=canonicalTime(f.binding.NotBefore); na,_:=canonicalTime(f.binding.NotAfter)
 return &casapi.CreateCertificateRequest{CSR:f.csr,Template:&x509.Certificate{SerialNumber:serial,Subject:f.csr.Subject,PublicKey:f.csr.PublicKey,NotBefore:nb,NotAfter:na,KeyUsage:x509.KeyUsageDigitalSignature,ExtKeyUsage:[]x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth},URIs:f.csr.URIs},Lifetime:certificateLifetime,Provisioner:&casapi.ProvisionerInfo{ID:f.c.Policy.ProvisionerID,Name:f.c.Policy.ProvisionerName,Type:"JWK"}}
}

func TestJournalCommitFailurePublishesNoLeafAndResumesExactRequest(t *testing.T) {
 f:=newJournalFixture(t)
 a,_,err:=f.j.Claim(f.binding); if err!=nil || a==nil {t.Fatalf("claim: %v",err)}
 f.j.BeforeCommit=func()error{return errors.New("injected durable storage failure")}
 response,err:=f.c.CreateCertificateWithContext(withAttempt(context.Background(),*a),f.request())
 if err==nil || response!=nil {t.Fatal("failed persistence returned usable certificate")}
 if _,err:=f.j.Get(certsTable,[]byte(f.binding.Serial)); !nosql.IsErrNotFound(err) {t.Fatalf("failed atomic receipt left certificate: %v",err)}
 r,err:=f.j.Observe(f.binding); if err!=nil || len(r.Chain)!=0 {t.Fatalf("failed commit published receipt: %v",err)}
 f.j.BeforeCommit=nil; f.now.Add(11)
 next,_,err:=f.j.Claim(f.binding); if err!=nil || next==nil || next.Epoch<=a.Epoch {t.Fatalf("resume: %v",err)}
 response,err=f.c.CreateCertificateWithContext(withAttempt(context.Background(),*next),f.request()); if err!=nil || response==nil {t.Fatalf("exact resume: %v",err)}
}

func TestJournalLostResponseAndRestartAdoptSameCommittedDER(t *testing.T) {
 f:=newJournalFixture(t)
 a,_,err:=f.j.Claim(f.binding); if err!=nil {t.Fatal(err)}
 response,err:=f.c.CreateCertificateWithContext(withAttempt(context.Background(),*a),f.request()); if err!=nil {t.Fatal(err)}
 original:=append([]byte(nil),response.Certificate.Raw...)
 // The response is deliberately discarded. A fresh boot must adopt the
 // committed effect, without invoking a second signer or changing its serial.
 if err:=f.j.Shutdown();err!=nil {t.Fatal(err)}
 reopened,err:=db.New(&db.Config{Type:"badgerv2",DataSource:f.path});if err!=nil {t.Fatal(err)}
 t.Cleanup(func(){_ = reopened.Shutdown()})
 restarted,err:=newJournalDB(reopened.(*db.DB),f.j.clock,f.j.lease); if err!=nil {t.Fatal(err)}
 replay,receipt,err:=restarted.Claim(f.binding); if err!=nil || replay==nil || len(receipt.Chain)==0 {t.Fatalf("restart adoption: %v",err)}
 committed,err:=restarted.ReadCommitted(*replay); if err!=nil || !bytes.Equal(committed[0].Raw,original) {t.Fatalf("lost response changed DER: %v",err)}
 if replay.Epoch!=a.Epoch {t.Fatal("committed receipt acquired a replacement epoch")}
}

func TestJournalConcurrentEpochRejectsOldCommitAndOldResponse(t *testing.T) {
 f:=newJournalFixture(t)
 old,_,err:=f.j.Claim(f.binding); if err!=nil {t.Fatal(err)}
 entered:=make(chan struct{}); release:=make(chan struct{})
 f.c.AfterSign=func(){close(entered);<-release}
 oldResult:=make(chan error,1)
 go func(){response,err:=f.c.CreateCertificateWithContext(withAttempt(context.Background(),*old),f.request());if response!=nil {oldResult<-errors.New("old signer returned leaf");return};oldResult<-err}()
 <-entered
 f.now.Add(11)
 fresh,_,err:=f.j.Claim(f.binding); if err!=nil || fresh==nil {t.Fatalf("next epoch: %v",err)}
 winner:=&JournalCAS{Journal:f.j,Soft:f.c.Soft,Policy:f.c.Policy}
 response,err:=winner.CreateCertificateWithContext(withAttempt(context.Background(),*fresh),f.request()); if err!=nil {t.Fatalf("winning signer: %v",err)}
 close(release)
 if err:=<-oldResult;err==nil {t.Fatal("old commit accepted")}
 if _,err:=f.j.ReadCommitted(*old);err==nil {t.Fatal("old response fence accepted winning epoch")}
 committed,err:=f.j.ReadCommitted(*fresh);if err!=nil || !bytes.Equal(committed[0].Raw,response.Certificate.Raw){t.Fatalf("winning receipt corrupted: %v",err)}
}

func TestJournalExactBindingAndSerialCannotAdoptAnotherRequest(t *testing.T) {
 f:=newJournalFixture(t)
 if _,_,err:=f.j.Claim(f.binding);err!=nil{t.Fatal(err)}
 variants:=[]Binding{f.binding,f.binding,f.binding,f.binding,f.binding}
 variants[0].Generation++;variants[1].NodeID="spk_"+strings.Repeat("b",32);variants[2].CSRSHA256=strings.Repeat("b",64);variants[3].PolicySHA256=strings.Repeat("b",64);variants[4].NotAfter="2099-01-01T00:00:00Z"
 for _,changed:=range variants {if _,err:=f.j.Observe(changed);err==nil{t.Fatal("changed binding adopted pending receipt")}}
 collision:=f.binding;collision.RequestID=strings.Repeat("b",43)
 if _,_,err:=f.j.Claim(collision);err==nil{t.Fatal("another request claimed exact reserved serial")}
}

func TestJournalRotationRevokedDuringSigningCannotCommitOrRespond(t *testing.T) {
 f:=newJournalFixture(t)
 source:="23";f.binding.Purpose="rotation";f.binding.SourceSerial=&source
 a,_,err:=f.j.Claim(f.binding);if err!=nil{t.Fatal(err)}
 f.c.AfterSign=func(){if err:=f.j.Revoke(&db.RevokedCertificateInfo{Serial:source,Reason:"test source revoked"});err!=nil{t.Fatal(err)}}
 response,err:=f.c.CreateCertificateWithContext(withAttempt(context.Background(),*a),f.request())
 if err==nil || response!=nil{t.Fatal("revoked source published replacement leaf")}
 if _,err:=f.j.Get(certsTable,[]byte(f.binding.Serial));!nosql.IsErrNotFound(err){t.Fatalf("revoked source left committed leaf: %v",err)}
}

func TestJournalRestartResumesAbandonedAttemptWithoutWaitingForOldLease(t *testing.T) {
 f:=newJournalFixture(t)
 old,_,err:=f.j.Claim(f.binding);if err!=nil{t.Fatal(err)}
 if err:=f.j.Shutdown();err!=nil{t.Fatal(err)}
 reopened,err:=db.New(&db.Config{Type:"badgerv2",DataSource:f.path});if err!=nil{t.Fatal(err)}
 t.Cleanup(func(){_ = reopened.Shutdown()})
 restarted,err:=newJournalDB(reopened.(*db.DB),f.j.clock,f.j.lease);if err!=nil{t.Fatal(err)}
 next,_,err:=restarted.Claim(f.binding)
 if err!=nil || next==nil || next.Owner==old.Owner || next.Epoch<=old.Epoch{t.Fatalf("restart did not fence abandoned owner: %v",err)}
 cas:=&JournalCAS{Journal:restarted,Soft:f.c.Soft,Policy:f.c.Policy}
 response,err:=cas.CreateCertificateWithContext(withAttempt(context.Background(),*next),f.request());if err!=nil || response==nil{t.Fatalf("restart exact resume: %v",err)}
 if _,err:=restarted.ReadCommitted(*old);err==nil{t.Fatal("restart accepted old response")}
}
