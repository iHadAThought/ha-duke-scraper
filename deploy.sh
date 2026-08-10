#!/usr/bin/env bash
# Deploy duke_scraper worker + custom component onto HAOS (VM 105) via Proxmox.
set -euo pipefail
export COPYFILE_DISABLE=1
PVE="${PVE:-root@172.16.1.5}"
ROOT="$(cd "$(dirname "$0")" && pwd)"

tar czf /tmp/duke_scraper_bundle.tgz -C "$ROOT" custom_components/duke_scraper worker
scp /tmp/duke_scraper_bundle.tgz "$PVE:/tmp/duke_scraper_bundle.tgz"

ssh "$PVE" 'python3 - << '"'"'PY'"'"'
import base64, pathlib, subprocess
raw=pathlib.Path("/tmp/duke_scraper_bundle.tgz").read_bytes()
b64=base64.b64encode(raw).decode()
remote="/tmp/duke_scraper_bundle.tgz"
subprocess.check_call(["qm","guest","exec","105","--timeout","30","--","bash","-lc",f"rm -f {remote} {remote}.b64"])
for i in range(0,len(b64),24000):
    part=b64[i:i+24000]; op=">" if i==0 else ">>"
    subprocess.check_call(["qm","guest","exec","105","--timeout","60","--","bash","-lc",f"printf %s {part} {op} {remote}.b64"])
print(subprocess.check_output(["qm","guest","exec","105","--timeout","60","--","bash","-lc",f"base64 -d {remote}.b64 > {remote} && rm {remote}.b64 && wc -c {remote}"], text=True))
PY'

ssh "$PVE" 'qm guest exec 105 --timeout 600 -- bash -lc "
set -e
HA=/mnt/data/supervisor/homeassistant
rm -rf /tmp/duke_scraper_extract
mkdir -p /tmp/duke_scraper_extract
tar xzf /tmp/duke_scraper_bundle.tgz -C /tmp/duke_scraper_extract
find /tmp/duke_scraper_extract -name \"._*\" -delete
mkdir -p \"\$HA/custom_components/duke_scraper\" \"\$HA/.duke_scraper\" \"\$HA/duke_scraper_worker\"
cp -a /tmp/duke_scraper_extract/custom_components/duke_scraper/. \"\$HA/custom_components/duke_scraper/\"
cp -a /tmp/duke_scraper_extract/worker/. \"\$HA/duke_scraper_worker/\"
cd \"\$HA/duke_scraper_worker\"
docker build -t duke_scraper_worker:local .
docker rm -f duke_scraper_worker 2>/dev/null || true
docker run -d --name duke_scraper_worker --restart unless-stopped \
  --network hassio --network-alias duke_scraper_worker \
  -v \"\$HA/.duke_scraper:/data\" \
  -e DUKE_SCRAPER_DATA=/data -e TZ=America/New_York \
  duke_scraper_worker:local
sleep 4
# Worker self-publishes worker_url on start (container IP). Fall back to inspect.
if [[ ! -s \"\$HA/.duke_scraper/worker_url\" ]]; then
  IP=\$(docker inspect duke_scraper_worker --format \"{{(index .NetworkSettings.Networks \\\"hassio\\\").IPAddress}}\")
  echo -n \"http://\$IP:8765\" > \"\$HA/.duke_scraper/worker_url\"
fi
echo worker_url=\$(cat \"\$HA/.duke_scraper/worker_url\")
curl -s \"\$(cat \"\$HA/.duke_scraper/worker_url\")/health\"
echo
docker exec hassio_cli ha core restart || true
"'
