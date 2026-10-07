package main

import (
 "bytes"
 "context"
 "crypto"
 "crypto/tls"
 "crypto/x509"
 "errors"
 "flag"
 "log"
 "net/http"
 "os"
 "os/signal"
 "syscall"
 "time"

 "github.com/smallstep/certificates/authority"
 "github.com/smallstep/certificates/authority/config"
 "github.com/smallstep/certificates/authority/provisioner"
 "github.com/smallstep/certificates/ca"
 casapi "github.com/smallstep/certificates/cas/apiv1"
 "github.com/smallstep/certificates/cas/softcas"
 "github.com/smallstep/certificates/db"
 "go.step.sm/crypto/pemutil"
)

const nodeTemplate = `{"subject":{"commonName":{{ toJson .Subject.CommonName }}},"sans":{{ toJson .SANs }},"keyUsage":["digitalSignature"],"extKeyUsage":["clientAuth"]}`

func configuredProvisioner(cfg *config.Config) (*provisioner.JWK,error) {
 if cfg.DB==nil || cfg.DB.Type!="badgerv2" || cfg.DB.DataSource=="" || cfg.AuthorityConfig==nil || cfg.AuthorityConfig.Options!=nil || cfg.AuthorityConfig.EnableAdmin || cfg.AuthorityConfig.DisableIssuedAtCheck || len(cfg.AuthorityConfig.Provisioners)!=1 || cfg.KMS!=nil || cfg.SSH!=nil || len(cfg.DNSNames)!=1 || cfg.DNSNames[0]!="step-ca" || cfg.InsecureAddress!="" {return nil,errors.New("CA configuration differs from the single journal authority")}
 if cfg.AuthorityConfig.Claims!=nil || cfg.AuthorityConfig.Policy!=nil || (cfg.AuthorityConfig.Template!=nil && *cfg.AuthorityConfig.Template!=(config.ASN1DN{})) {return nil,errors.New("CA authority policy differs from fixed node profile")}
 p,ok:=cfg.AuthorityConfig.Provisioners[0].(*provisioner.JWK)
 if !ok || p.Name!="vonk-forge-agent" || p.Key==nil || p.Key.KeyID=="" || !p.Key.IsPublic() || p.Key.Algorithm!="ES256" || p.EncryptedKey!="" || p.Claims==nil || p.Claims.MinTLSDur==nil || p.Claims.MaxTLSDur==nil || p.Claims.DefaultTLSDur==nil || p.Claims.MinTLSDur.Duration!=certificateLifetime || p.Claims.MaxTLSDur.Duration!=certificateLifetime || p.Claims.DefaultTLSDur.Duration!=certificateLifetime || p.Claims.DisableRenewal==nil || !*p.Claims.DisableRenewal || p.Claims.DisableSmallstepExtensions==nil || !*p.Claims.DisableSmallstepExtensions {return nil,errors.New("CA provisioner differs from fixed node policy")}
 if p.Options==nil || p.Options.X509==nil || p.Options.X509.Template!=nodeTemplate || p.Options.X509.TemplateFile!="" || len(p.Options.X509.TemplateData)!=0 || p.Options.SSH!=nil || p.Options.Wire!=nil || len(p.Options.Webhooks)!=0 {return nil,errors.New("CA certificate profile differs from fixed node policy")}
 return p,nil
}

func openAuthority(cfg *config.Config,password []byte) (*authority.Authority,*Service,error) {
 p,err:=configuredProvisioner(cfg);if err!=nil{return nil,nil,err}
 issuer,err:=pemutil.ReadCertificate(cfg.IntermediateCert);if err!=nil{return nil,nil,err}
 key,err:=pemutil.Read(cfg.IntermediateKey,pemutil.WithPassword(password));if err!=nil{return nil,nil,err}
 signer,ok:=key.(crypto.Signer);if !ok{return nil,nil,errors.New("CA issuer key cannot sign")}
 issuerPublic,err:=x509.MarshalPKIXPublicKey(issuer.PublicKey);if err!=nil{return nil,nil,err}
 signerPublic,err:=x509.MarshalPKIXPublicKey(signer.Public());if err!=nil || !bytes.Equal(issuerPublic,signerPublic){return nil,nil,errors.New("CA issuer key differs from certificate")}
 soft,err:=softcas.New(context.Background(),casapi.Options{CertificateChain:[]*x509.Certificate{issuer},Signer:signer});if err!=nil{return nil,nil,err}
 authdb,err:=db.New(cfg.DB);if err!=nil{return nil,nil,err}
 base,ok:=authdb.(*db.DB);if !ok{_ = authdb.Shutdown();return nil,nil,errors.New("journal requires persistent Smallstep DB")}
 journal,err:=newJournalDB(base,time.Now,30*time.Second);if err!=nil{_ = authdb.Shutdown();return nil,nil,err}
 policy:=Policy{Issuer:issuer,ProvisionerName:p.Name,ProvisionerKID:p.Key.KeyID,ProvisionerID:p.GetID(),ServerNames:append([]string(nil),cfg.DNSNames...)}
 cas:=&JournalCAS{Journal:journal,Soft:soft,Policy:policy}
 auth,err:=authority.New(cfg,authority.WithDatabase(journal),authority.WithX509CAService(cas),authority.WithX509IntermediateCerts(issuer),authority.WithPassword(password),authority.WithQuietInit())
 if err!=nil{_ = authdb.Shutdown();return nil,nil,err}
 return auth,NewService(auth,cas,journal,policy),nil
}

func run() error {
 configPath:=flag.String("config","","existing Smallstep CA configuration")
 passwordFile:=flag.String("password-file","","existing issuer key password file")
 flag.Parse()
 if *configPath=="" || *passwordFile=="" || flag.NArg()!=0{return errors.New("--config and --password-file are required")}
 cfg,err:=config.LoadConfiguration(*configPath);if err!=nil{return err}
 password,err:=os.ReadFile(*passwordFile);if err!=nil{return err}
 auth,service,err:=openAuthority(cfg,bytes.TrimSpace(password));if err!=nil{return err}
 defer auth.Shutdown()
 initial,err:=auth.GetTLSCertificate();if err!=nil{return err}
 renewer,err:=ca.NewTLSRenewer(initial,auth.GetTLSCertificate);if err!=nil{return err}
 ctx,stop:=signal.NotifyContext(context.Background(),syscall.SIGTERM,syscall.SIGINT);defer stop()
 renewer.RunContext(ctx);defer renewer.Stop()
 tlsConfig:=&tls.Config{MinVersion:tls.VersionTLS12}
 if cfg.TLS!=nil{tlsConfig=cfg.TLS.TLSConfig();if tlsConfig.MinVersion<tls.VersionTLS12{tlsConfig.MinVersion=tls.VersionTLS12}}
 tlsConfig.Certificates=nil;tlsConfig.GetCertificate=renewer.GetCertificateForCA
 server:=&http.Server{Addr:cfg.Address,Handler:service,TLSConfig:tlsConfig,ReadHeaderTimeout:10*time.Second,ReadTimeout:15*time.Second,WriteTimeout:45*time.Second,IdleTimeout:60*time.Second,MaxHeaderBytes:16*1024}
 go func(){<-ctx.Done();shutdown,cancel:=context.WithTimeout(context.Background(),10*time.Second);defer cancel();_ = server.Shutdown(shutdown)}()
 err=server.ListenAndServeTLS("","")
 if errors.Is(err,http.ErrServerClosed){return nil};return err
}

func main(){if err:=run();err!=nil{log.Print(err);os.Exit(1)}}
