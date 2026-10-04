#!/usr/bin/env bash
set -euo pipefail

install -d -m 0755 /opt/alpha-relay /var/www/alpha-radar
install -d -o www-data -g www-data -m 0750 /var/lib/alpha-relay
install -m 0755 /tmp/alpha_readonly_cloud_relay.py /opt/alpha-relay/alpha_readonly_cloud_relay.py
install -m 0644 /tmp/alpha-readonly-relay.service /etc/systemd/system/alpha-readonly-relay.service
install -m 0600 /tmp/alpha-relay.env /etc/alpha-relay.env

rm -rf /var/www/alpha-radar/*
tar -xzf /tmp/alpha-radar-web.tar.gz -C /var/www/alpha-radar
chown -R root:root /var/www/alpha-radar

if [ -f /etc/nginx/sites-available/clash-sub ]; then
  cp /etc/nginx/sites-available/clash-sub /etc/nginx/sites-available/clash-sub.before-alpha-radar
fi
install -m 0644 /tmp/alpha-radar.nginx.conf /etc/nginx/sites-available/clash-sub

python3 -m py_compile /opt/alpha-relay/alpha_readonly_cloud_relay.py
systemctl daemon-reload
systemctl enable --now alpha-readonly-relay
nginx -t
systemctl reload nginx

systemctl is-active alpha-readonly-relay
systemctl is-active nginx
systemctl is-active xray
