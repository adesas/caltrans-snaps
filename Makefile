PY = PYTHONPATH=src python3

.PHONY: catalog run test retention-sweep site-serve

catalog:  ## regenerate camera config + site data from Caltrans ArcGIS API
	$(PY) -m snaps.catalog --counties Butte Yuba Sutter \
	  --near 39.5124,-121.5417 --prefer-route SR-70 \
	  --demote Chico Skyway Southgate Eaton Garner "East Ave" "East 20th" "Hwy 32" \
	  --limit 10

run:  ## record to storage_root from config/cameras.yaml (foreground)
	$(PY) -m snaps.main --config config/cameras.yaml

retention-sweep:  ## one-shot cleanup of expired hours (also runs in-process)
	$(PY) -c "from pathlib import Path; from snaps.retention import sweep_once; import yaml; c=yaml.safe_load(open('config/cameras.yaml')); print(sweep_once(Path(c['storage_root']), int(c['recording']['retention_hours'])), 'dirs removed')"

site-serve:  ## preview the site locally (needs local recordings for playback)
	python3 -m http.server 8080 -d web

install-service:  ## install systemd unit (uses sudo)
	sudo cp deploy/caltrans-snaps.service /etc/systemd/system/
	sudo systemctl daemon-reload && sudo systemctl enable --now caltrans-snaps
