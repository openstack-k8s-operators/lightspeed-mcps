#!/bin/bash

eval $(crc oc-env)

# Get the TLS CA bundle used by the OpenStack client
# (we could also get it from the secret, but the command is less clear)
oc -n openstack cp openstackclient:/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem ./tls-ca-bundle.pem

# Get the OpenStack credentials
oc -n openstack cp openstackclient:/home/cloud-admin/.config/openstack/clouds.yaml ./clouds.yaml
oc -n openstack cp openstackclient:/home/cloud-admin/.config/openstack/secure.yaml ./secure.yaml
