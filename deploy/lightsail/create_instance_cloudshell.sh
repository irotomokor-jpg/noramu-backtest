#!/usr/bin/env bash
set -euo pipefail

# Run from AWS CloudShell after reviewing the monthly Lightsail charge.
# Creates one Ubuntu Lightsail instance + one attached Static IPv4 in Seoul.
# It does NOT configure Toss credentials and does NOT enable trading.

REGION="${AWS_REGION:-ap-northeast-2}"
AZ="${LIGHTSAIL_AZ:-ap-northeast-2a}"
INSTANCE="${LIGHTSAIL_INSTANCE:-noramu-shadow}"
STATIC_IP_NAME="${LIGHTSAIL_STATIC_IP:-noramu-shadow-ip}"
BUNDLE_ID="${LIGHTSAIL_BUNDLE:-micro_3_0}"

if aws lightsail get-instance --instance-name "$INSTANCE" --region "$REGION" >/dev/null 2>&1; then
  echo "Instance $INSTANCE already exists; refusing to create a duplicate."
  exit 2
fi

# Resolve an active Ubuntu 24.04 OS blueprint dynamically. Fall back to ubuntu_24_04 if the name query changes.
BLUEPRINT_ID="$(aws lightsail get-blueprints --region "$REGION" \
  --query "blueprints[?isActive==\`true\` && platform=='LINUX_UNIX' && type=='os' && contains(name, 'Ubuntu') && contains(version, '24.04')].blueprintId | [0]" \
  --output text 2>/dev/null || true)"
if [[ -z "$BLUEPRINT_ID" || "$BLUEPRINT_ID" == "None" ]]; then
  BLUEPRINT_ID="ubuntu_24_04"
fi

PRICE="$(aws lightsail get-bundles --region "$REGION" --query "bundles[?bundleId=='$BUNDLE_ID'].price | [0]" --output text)"
RAM="$(aws lightsail get-bundles --region "$REGION" --query "bundles[?bundleId=='$BUNDLE_ID'].ramSizeInGb | [0]" --output text)"
IPV4="$(aws lightsail get-bundles --region "$REGION" --query "bundles[?bundleId=='$BUNDLE_ID'].publicIpv4AddressCount | [0]" --output text)"

if [[ "$IPV4" != "1" ]]; then
  echo "Selected bundle does not include public IPv4; aborting."
  exit 3
fi

cat <<EOF
About to create:
  region:      $REGION
  zone:        $AZ
  instance:    $INSTANCE
  blueprint:   $BLUEPRINT_ID
  bundle:      $BUNDLE_ID
  RAM:         ${RAM} GB
  price:       USD ${PRICE}/month (Lightsail bundle price; taxes/other usage may apply)
  static IPv4: attached to instance
EOF

read -r -p "Type CREATE to continue: " ANSWER
[[ "$ANSWER" == "CREATE" ]] || { echo "Cancelled."; exit 1; }

aws lightsail create-instances \
  --instance-names "$INSTANCE" \
  --availability-zone "$AZ" \
  --blueprint-id "$BLUEPRINT_ID" \
  --bundle-id "$BUNDLE_ID" \
  --region "$REGION" \
  --tags key=project,value=noramu-shadow key=mode,value=read-only

for _ in $(seq 1 60); do
  STATE="$(aws lightsail get-instance-state --instance-name "$INSTANCE" --region "$REGION" --query 'state.name' --output text 2>/dev/null || true)"
  [[ "$STATE" == "running" ]] && break
  sleep 5
done

STATE="$(aws lightsail get-instance-state --instance-name "$INSTANCE" --region "$REGION" --query 'state.name' --output text)"
[[ "$STATE" == "running" ]] || { echo "Instance did not reach running state."; exit 4; }

aws lightsail allocate-static-ip --static-ip-name "$STATIC_IP_NAME" --region "$REGION"
aws lightsail attach-static-ip --static-ip-name "$STATIC_IP_NAME" --instance-name "$INSTANCE" --region "$REGION"

STATIC_IP="$(aws lightsail get-static-ip --static-ip-name "$STATIC_IP_NAME" --region "$REGION" --query 'staticIp.ipAddress' --output text)"

echo
printf 'LIGHTSAIL_CREATE=PASS\nINSTANCE=%s\nSTATIC_IPV4=%s\n' "$INSTANCE" "$STATIC_IP"
echo "Register STATIC_IPV4 in Toss WTS > Settings > Open API allowed-IP settings."
echo "Then open the Lightsail browser SSH terminal for $INSTANCE and run deploy/lightsail/bootstrap.sh from this repository."
