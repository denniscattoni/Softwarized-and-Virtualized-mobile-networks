#!/usr/bin/env bash
set -euo pipefail

# Generate a self-signed server certificate for NGINX with SAN for:
# - VIP IP: 10.0.0.100
# - DNS names: srv1, srv2, nginx, localhost
#
# Outputs (in this directory):
# - server.key
# - server.crt
# - server.pem
# - openssl.cnf
# - cert-info.txt

CERT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VIP_IP="10.0.0.100"
LISTEN_DNS_1="srv1"
LISTEN_DNS_2="srv2"
LISTEN_DNS_3="nginx"
LISTEN_DNS_4="localhost"

DAYS_VALID=365

CN="${VIP_IP}"

cat > "${CERT_DIR}/openssl.cnf" <<EOF
[ req ]
prompt             = no
distinguished_name = dn
x509_extensions    = v3_req

[ dn ]
CN = ${CN}

[ v3_req ]
# Minimal v3 extensions for a TLS server certificate
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid,issuer
basicConstraints = critical,CA:TRUE
keyUsage = critical,digitalSignature,keyEncipherment,keyCertSign
extendedKeyUsage = serverAuth
subjectAltName = @alt_names

[ alt_names ]
IP.1  = ${VIP_IP}
DNS.1 = ${LISTEN_DNS_1}
DNS.2 = ${LISTEN_DNS_2}
DNS.3 = ${LISTEN_DNS_3}
DNS.4 = ${LISTEN_DNS_4}
EOF

# Private key (Ed25519)
openssl genpkey -algorithm Ed25519 -out "${CERT_DIR}/server.key"

# Self-signed certificate with SAN
openssl req -x509 \
  -new \
  -key "${CERT_DIR}/server.key" \
  -out "${CERT_DIR}/server.crt" \
  -days "${DAYS_VALID}" \
  -config "${CERT_DIR}/openssl.cnf" \
  -extensions v3_req

# Convenience bundle (optional)
cat "${CERT_DIR}/server.crt" "${CERT_DIR}/server.key" > "${CERT_DIR}/server.pem"

# optional info, useful for checking SAN quickly
openssl x509 -in "${CERT_DIR}/server.crt" -noout -text > "${CERT_DIR}/cert-info.txt"
