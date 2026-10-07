module github.com/CarstVaartjes/vonk-forge/step-ca

go 1.25.0

require (
	github.com/smallstep/certificates v0.30.2
	github.com/smallstep/nosql v0.8.0
	go.step.sm/crypto v0.77.1
)

replace github.com/smallstep/certificates => ../upstream/certificates
