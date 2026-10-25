# x728 watchdog service

A single Python daemon (libgpiod v2 + smbus2) that replaces `xPWR.sh` and the
sample scripts on an X728 v2 board:

- hardware button: short press reboots, long press shuts down
- AC loss detection on the PLD pin (BCM 6)
- battery warning beeps and a clean shutdown at the critical level
- periodic push webhooks (Uptime Kuma and/or Tianji) for AC state and battery

## Install

```sh
sudo apt install python3-smbus2
# the apt python3-libgpiod is still v1 on Ubuntu 24.04; the script needs v2
sudo pip3 install --break-system-packages 'gpiod>=2'
sudo mkdir -p /opt/x728
sudo cp watchdog.py /opt/x728/
sudo cp x728.conf.example /opt/x728/x728.conf   # then fill in the webhooks
sudo cp x728-pwr.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now x728-pwr
```

Logs go to the journal: `journalctl -u x728-pwr`.
