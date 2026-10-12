# Serve the HLS tree for the website. Caddy sets correct MIME types and we
# add CORS so the GitHub Pages origin can fetch segments. Port 8090 is fed
# to cloudflared (see cloudflared-config.example.yml). Run with:
#   caddy file-server --listen :8090 --root /mnt/8tb/caltrans-snaps &
# or as a systemd unit. Equivalent nginx snippet for variety-minded people:
#   location / { root /mnt/8tb/caltrans-snaps; types { } include /etc/nginx/mime.types;
#     add_header Access-Control-Allow-Origin *; }
