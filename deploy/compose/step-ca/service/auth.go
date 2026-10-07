package main

import (
 "bytes"
 "crypto/sha256"
 "crypto/x509"
 "encoding/base64"
 "encoding/json"
 "errors"
 "io"
 "reflect"
 "strings"

 "go.step.sm/crypto/jose"
)

func strictJSON(raw []byte, dst any) error {
 if err:=uniqueJSONKeys(json.NewDecoder(bytes.NewReader(raw)));err!=nil{return err}
 decoder:=json.NewDecoder(bytes.NewReader(raw)); decoder.DisallowUnknownFields()
 if err:=decoder.Decode(dst);err!=nil{return err}
 if err:=decoder.Decode(new(any));err!=io.EOF{return errors.New("multiple JSON documents")}
 return nil
}

func uniqueJSONKeys(decoder *json.Decoder) error {
 token,err:=decoder.Token();if err!=nil{return err}
 delimiter,ok:=token.(json.Delim);if !ok{return nil}
 switch delimiter {
 case '{':
  keys:=map[string]bool{}
  for decoder.More() {
   key,err:=decoder.Token();if err!=nil{return err}
   name,ok:=key.(string);if !ok || keys[name]{return errors.New("duplicate JSON key")};keys[name]=true
   if err:=uniqueJSONKeys(decoder);err!=nil{return err}
  }
 case '[':
  for decoder.More(){if err:=uniqueJSONKeys(decoder);err!=nil{return err}}
 default:return errors.New("invalid JSON delimiter")
 }
 _,err=decoder.Token();return err
}

// Required-field presence is derived from the Binding's typed fields, including
// required nullable source_serial. Missing null must never become implicit null.
func bindingJSON(raw []byte) (Binding,error) {
 var binding Binding
 if err:=strictJSON(raw,&binding);err!=nil{return binding,err}
 var fields map[string]json.RawMessage
 if err:=json.Unmarshal(raw,&fields);err!=nil{return binding,err}
 kind:=reflect.TypeOf(binding)
 for i:=0;i<kind.NumField();i++ {
  name:=strings.Split(kind.Field(i).Tag.Get("json"),",")[0]
  if value,ok:=fields[name];!ok || (bytes.Equal(bytes.TrimSpace(value),[]byte("null")) && kind.Field(i).Type.Kind()!=reflect.Pointer) {return binding,errors.New("missing required binding field")}
 }
 return binding,nil
}

// Call only after Authority.Authorize has authenticated these exact JWT bytes.
// This parser adds the durable Vonk binding; it does not replace JWT policy.
func authenticatedBinding(ott string, body Binding, csr *x509.CertificateRequest) error {
 token,err:=jose.ParseSigned(ott);if err!=nil{return err}
 payload,err:=base64.RawURLEncoding.DecodeString(strings.Split(ott,".")[1]);if err!=nil{return err}
 if err:=uniqueJSONKeys(json.NewDecoder(bytes.NewReader(payload)));err!=nil{return err}
 var claims struct {
  Subject string `json:"sub"`
  ID string `json:"jti"`
  SANs []string `json:"sans"`
  Vonk json.RawMessage `json:"vonk"`
  Confirmation map[string]string `json:"cnf"`
 }
 if err:=token.UnsafeClaimsWithoutVerification(&claims);err!=nil{return err}
 signed,err:=bindingJSON(claims.Vonk);if err!=nil{return err}
 sum:=sha256.Sum256(csr.Raw)
 confirmation:=base64.RawURLEncoding.EncodeToString(sum[:])
 if !sameBinding(signed,body) || claims.Subject!=body.NodeID || claims.ID=="" || claims.ID==body.RequestID || len(claims.SANs)!=1 || claims.SANs[0]!="spiffe://vonk-forge.local/node/"+body.NodeID || len(claims.Confirmation)!=1 || claims.Confirmation["x5rt#S256"]!=confirmation {return errors.New("JWT does not bind the exact certificate request")}
 return nil
}
