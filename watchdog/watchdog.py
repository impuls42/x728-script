#!/usr/bin/env python3
"""
X728 UPS Power Management and Watchdog Service

Handles:
- Button press detection (short press = reboot, long press = shutdown)
- AC power loss detection with battery monitoring
- Battery level warnings (beeping) and critical shutdown
- Continuous watchdog status reporting to webhook
"""

import gpiod
from gpiod.line import Direction, Value, Edge, Bias
from gpiod import EdgeEvent
import smbus2
import struct
import time
import threading
import signal
import sys
import subprocess
import urllib.parse
import urllib.request
import configparser
from datetime import datetime
from pathlib import Path


class X728Service:
    def __init__(self, config_path="/opt/x728/x728.conf"):
        """Initialize the X728 service with configuration."""
        self.config = configparser.ConfigParser()
        self.name = "X728-Service"
        
        # Try multiple config locations
        config_paths = [
            config_path,
            Path(__file__).parent / "x728.conf",
            "/etc/x728.conf"
        ]
        
        config_loaded = False
        for path in config_paths:
            if Path(path).exists():
                self.config.read(path)
                config_loaded = True
                self.log(f"Loaded config from {path}")
                break
        
        if not config_loaded:
            self.log("ERROR: No configuration file found!")
            sys.exit(1)
        
        # Load configuration
        self.load_config()
        
        # State variables
        self.ac_power_ok = True
        self.ac_last_change = time.monotonic()
        self.battery_voltage = 0.0
        self.battery_capacity = 0.0
        self.warning_active = False
        self.running = True
        
        # Initialize hardware
        self.init_gpio()
        self.init_i2c()
        
        # Setup signal handlers
        signal.signal(signal.SIGTERM, self.signal_handler)
        signal.signal(signal.SIGINT, self.signal_handler)
    
    def load_config(self):
        """Load configuration from config file."""
        # GPIO settings
        self.gpiochip_name = self.config.get('GPIO', 'GPIOCHIP', fallback='gpiochip0')
        self.pin_pld = self.config.getint('GPIO', 'PIN_PLD', fallback=6)
        self.pin_shutdown = self.config.getint('GPIO', 'PIN_SHUTDOWN', fallback=5)
        self.pin_boot = self.config.getint('GPIO', 'PIN_BOOT', fallback=12)
        self.pin_buzzer = self.config.getint('GPIO', 'PIN_BUZZER', fallback=20)
        self.pin_button = self.config.getint('GPIO', 'PIN_BUTTON', fallback=26)
        
        # Battery settings
        self.warn_level = self.config.getint('Battery', 'WARN_LEVEL', fallback=20)
        self.critical_level = self.config.getint('Battery', 'CRITICAL_LEVEL', fallback=10)
        self.beep_enabled = self.config.getboolean('Battery', 'BEEP_ENABLED', fallback=True)
        self.beep_interval = self.config.getint('Battery', 'BEEP_INTERVAL', fallback=30)
        # Seconds AC must stay lost before a critical shutdown. The X728 AON
        # jumper powers the Pi on only on an AC edge, so shutting down while
        # AC is coming back leaves the Pi off until the next outage ends.
        self.ac_settle = self.config.getint('Battery', 'AC_SETTLE', fallback=30)
        
        # Watchdog settings. Every webhook is optional: an empty or missing one
        # is skipped. Uptime Kuma's (AC_WEBHOOK, DEVICE_WEBHOOK) take
        # status/message/ping; Tianji's (TIANJI_*) take status/msg/value.
        self.ac_webhook = self.config.get('Watchdog', 'AC_WEBHOOK', fallback='')
        self.device_webhook = self.config.get('Watchdog', 'DEVICE_WEBHOOK', fallback='')
        self.tianji_ac_webhook = self.config.get('Watchdog', 'TIANJI_AC_WEBHOOK', fallback='')
        self.tianji_device_webhook = self.config.get('Watchdog', 'TIANJI_DEVICE_WEBHOOK', fallback='')
        self.webhook_interval = self.config.getint('Watchdog', 'INTERVAL', fallback=60)
        
        # I2C settings
        self.i2c_bus = self.config.getint('I2C', 'BUS', fallback=1)
        self.i2c_addr = int(self.config.get('I2C', 'ADDR', fallback='0x36'), 16)
    
    def init_gpio(self):
        """Initialize GPIO lines using gpiod v2.x API."""
        chip_path = f"/dev/{self.gpiochip_name}"
        
        # Request output lines
        self.output_request = gpiod.request_lines(
            chip_path,
            consumer=self.name,
            config={
                self.pin_boot: gpiod.LineSettings(
                    direction=Direction.OUTPUT,
                    output_value=Value.ACTIVE  # Enable button reading
                ),
                self.pin_buzzer: gpiod.LineSettings(
                    direction=Direction.OUTPUT,
                    output_value=Value.INACTIVE  # Buzzer off
                ),
                self.pin_button: gpiod.LineSettings(
                    direction=Direction.OUTPUT,
                    output_value=Value.INACTIVE  # Button not pressed
                ),
            },
        )
        
        # Request input lines with edge detection (separate requests to avoid race conditions)
        self.shutdown_request = gpiod.request_lines(
            chip_path,
            consumer=f"{self.name}-shutdown",
            config={
                self.pin_shutdown: gpiod.LineSettings(
                    direction=Direction.INPUT,
                    edge_detection=Edge.BOTH,
                    bias=Bias.PULL_DOWN,
                    event_clock=gpiod.line.Clock.MONOTONIC
                ),
            },
        )
        
        self.pld_request = gpiod.request_lines(
            chip_path,
            consumer=f"{self.name}-pld",
            config={
                self.pin_pld: gpiod.LineSettings(
                    direction=Direction.INPUT,
                    edge_detection=Edge.BOTH,
                    bias=Bias.PULL_DOWN,
                    event_clock=gpiod.line.Clock.MONOTONIC
                ),
            },
        )
        
        self.log("GPIO initialized")
    
    def init_i2c(self):
        """Initialize I2C bus for battery monitoring."""
        try:
            self.i2c = smbus2.SMBus(self.i2c_bus)
            self.log("I2C initialized")
        except Exception as e:
            self.log(f"ERROR: Failed to initialize I2C: {e}")
            self.i2c = None
    
    def push(self, label, url, params, timeout=10):
        """Send one push. A missing URL is skipped; a failure is logged, never raised."""
        if not url:
            return
        try:
            urllib.request.urlopen(f"{url}?{urllib.parse.urlencode(params)}", timeout=timeout)
            self.log(f"{label} push: {params}")
        except Exception as e:
            self.log(f"ERROR {label} push: {e}")

    def log(self, message):
        """Log message with timestamp."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] {message}", flush=True)
    
    def read_battery_voltage(self):
        """Read battery voltage from I2C fuel gauge."""
        if not self.i2c:
            return 0.0
        
        try:
            read = self.i2c.read_word_data(self.i2c_addr, 2)
            swapped = struct.unpack("<H", struct.pack(">H", read))[0]
            voltage = swapped * 1.25 / 1000 / 16
            return voltage
        except Exception as e:
            self.log(f"ERROR reading voltage: {e}")
            return 0.0
    
    def read_battery_capacity(self):
        """Read battery capacity (percentage) from I2C fuel gauge."""
        if not self.i2c:
            return 0.0
        
        try:
            read = self.i2c.read_word_data(self.i2c_addr, 4)
            swapped = struct.unpack("<H", struct.pack(">H", read))[0]
            capacity = swapped / 256
            if capacity > 100:
                capacity = 100
            return capacity
        except Exception as e:
            self.log(f"ERROR reading capacity: {e}")
            return 0.0
    
    def beep(self, duration=0.5):
        """Sound the buzzer for specified duration."""
        if not self.beep_enabled:
            return
        try:
            self.output_request.set_value(self.pin_buzzer, Value.ACTIVE)
            time.sleep(duration)
            self.output_request.set_value(self.pin_buzzer, Value.INACTIVE)
        except Exception as e:
            self.log(f"ERROR beeping: {e}")
    
    def trigger_shutdown(self):
        """Trigger system shutdown by pulsing button pin."""
        self.log("Triggering shutdown sequence...")
        try:
            self.output_request.set_value(self.pin_button, Value.ACTIVE)
            time.sleep(3)
            self.output_request.set_value(self.pin_button, Value.INACTIVE)
            time.sleep(1)
            subprocess.run(['sudo', 'poweroff'], check=False)
        except Exception as e:
            self.log(f"ERROR triggering shutdown: {e}")
    
    def trigger_reboot(self):
        """Trigger system reboot by pulsing button pin."""
        self.log("Triggering reboot sequence...")
        try:
            self.output_request.set_value(self.pin_button, Value.ACTIVE)
            time.sleep(3)
            self.output_request.set_value(self.pin_button, Value.INACTIVE)
            time.sleep(1)
            subprocess.run(['sudo', 'reboot'], check=False)
        except Exception as e:
            self.log(f"ERROR triggering reboot: {e}")
    
    def battery_monitor_thread(self):
        """Monitor battery levels and trigger warnings/shutdown."""
        self.log("Battery monitor thread started")
        last_beep_time = 0
        holding = False
        
        while self.running:
            try:
                self.battery_voltage = self.read_battery_voltage()
                self.battery_capacity = self.read_battery_capacity()
                
                # Check critical level
                if self.battery_capacity < self.critical_level and not self.ac_power_ok:
                    # Hold while AC is flapping: it may be coming back
                    lost_for = time.monotonic() - self.ac_last_change
                    if lost_for < self.ac_settle:
                        if not holding:
                            self.log(f"CRITICAL: Battery at {self.battery_capacity:.1f}% - AC changed {lost_for:.0f}s ago, "
                                     f"holding shutdown until AC stays lost for {self.ac_settle}s")
                            holding = True
                        time.sleep(1)
                        continue
                    holding = False
                    self.log(f"CRITICAL: Battery at {self.battery_capacity:.1f}% - AC lost for {lost_for:.0f}s, initiating shutdown")
                    
                    # Send emergency device webhook notifications
                    voltage = f"{self.battery_voltage:.2f}V"
                    capacity = f"{self.battery_capacity:.0f}"
                    self.push("Emergency device (Kuma)", self.device_webhook,
                              {"status": "down", "message": voltage, "ping": capacity}, timeout=5)
                    self.push("Emergency device (Tianji)", self.tianji_device_webhook,
                              {"status": "down", "msg": voltage, "value": capacity}, timeout=5)
                    
                    self.beep(0.1)
                    time.sleep(0.2)
                    self.beep(0.1)
                    time.sleep(0.2)
                    self.beep(0.1)
                    time.sleep(5)
                    if self.ac_power_ok:
                        self.log("AC Power RESTORED during shutdown countdown - shutdown aborted")
                        continue
                    self.trigger_shutdown()
                    break
                
                # Check warning level
                elif self.battery_capacity < self.warn_level and not self.ac_power_ok:
                    if not self.warning_active:
                        self.log(f"WARNING: Battery at {self.battery_capacity:.1f}% - starting warning beeps")
                        self.warning_active = True
                    
                    # Periodic beeping
                    current_time = time.time()
                    if current_time - last_beep_time >= self.beep_interval:
                        self.beep(0.5)
                        last_beep_time = current_time
                        self.log(f"Battery: {self.battery_capacity:.1f}% ({self.battery_voltage:.2f}V)")
                
                else:
                    if holding:
                        self.log("AC Power back - critical shutdown cancelled")
                        holding = False
                    if self.warning_active:
                        self.log("Battery level OK - stopping warnings")
                        self.warning_active = False
                
                time.sleep(2)
                
            except Exception as e:
                self.log(f"ERROR in battery monitor: {e}")
                time.sleep(5)
    
    def button_monitor_thread(self):
        """Monitor hardware button press for shutdown/reboot."""
        self.log("Button monitor thread started")
        REBOOT_MIN = 0.2  # 200ms
        REBOOT_MAX = 0.6  # 600ms
        
        press_start_time = None
        
        while self.running:
            try:
                # Wait for button press event
                if self.shutdown_request.wait_edge_events(timeout=1.0):
                    events = self.shutdown_request.read_edge_events(max_events=16)
                    
                    for event in events:
                        if event.line_offset == self.pin_shutdown:
                            # Use proper EdgeEvent.Type enum comparison
                            if event.event_type == EdgeEvent.Type.RISING_EDGE:
                                # Button pressed
                                press_start_time = time.time()
                                self.log("Button press detected")
                                
                            elif event.event_type == EdgeEvent.Type.FALLING_EDGE and press_start_time is not None:
                                # Button released
                                press_duration = time.time() - press_start_time
                                press_start_time = None
                                
                                if press_duration >= REBOOT_MAX:
                                    # Long press - shutdown
                                    self.log(f"Long button press ({press_duration:.2f}s) - shutting down")
                                    self.beep(0.1)
                                    time.sleep(0.3)
                                    self.beep(0.1)
                                    self.trigger_shutdown()
                                    return
                                    
                                elif press_duration >= REBOOT_MIN:
                                    # Short press - reboot
                                    self.log(f"Short button press ({press_duration:.2f}s) - rebooting")
                                    self.beep(0.1)
                                    self.trigger_reboot()
                                    return
                                else:
                                    self.log(f"Button press too short ({press_duration:.2f}s) - ignored")
                            
            except TimeoutError:
                # No events, continue
                # Check for stuck button (pressed > REBOOT_MAX without release event)
                if press_start_time and (time.time() - press_start_time) > REBOOT_MAX:
                    self.log("Long button press timeout - shutting down")
                    self.beep(0.1)
                    time.sleep(0.3)
                    self.beep(0.1)
                    self.trigger_shutdown()
                    return
            except Exception as e:
                self.log(f"ERROR in button monitor: {e}")
                time.sleep(1)
    
    def power_monitor_thread(self):
        """Monitor AC power loss/restoration."""
        self.log("Power monitor thread started")
        
        # Read initial power state
        try:
            initial_state = self.pld_request.get_value(self.pin_pld)
            self.ac_power_ok = (initial_state == Value.INACTIVE)
            self.log(f"Initial AC power state: {'OK' if self.ac_power_ok else 'LOST'} (PLD={initial_state})")
        except Exception as e:
            self.log(f"ERROR reading initial power state: {e}")
        
        while self.running:
            try:
                # Wait for power change event
                if self.pld_request.wait_edge_events(timeout=1.0):
                    events = self.pld_request.read_edge_events(max_events=16)
                    
                    for event in events:
                        if event.line_offset == self.pin_pld:
                            # Use proper EdgeEvent.Type enum comparison
                            if event.event_type == EdgeEvent.Type.RISING_EDGE:
                                # Power lost (PLD goes high)
                                self.ac_power_ok = False
                                self.ac_last_change = time.monotonic()
                                self.log("AC Power LOST - running on battery")
                                self.beep(0.5)
                                
                            elif event.event_type == EdgeEvent.Type.FALLING_EDGE:
                                # Power restored (PLD goes low)
                                self.ac_power_ok = True
                                self.ac_last_change = time.monotonic()
                                self.warning_active = False
                                self.log("AC Power RESTORED")
                                self.beep(0.1)
                                time.sleep(0.2)
                                self.beep(0.1)
                                time.sleep(0.2)
                                self.beep(0.1)
                        
            except TimeoutError:
                # No events, continue
                pass
            except Exception as e:
                self.log(f"ERROR in power monitor: {e}")
                time.sleep(1)
    
    def watchdog_thread(self):
        """Send periodic status updates to webhooks."""
        self.log("Watchdog thread started")
        
        while self.running:
            # Wait for next interval
            time.sleep(self.webhook_interval)
            # AC power status (up/down from the AC sensor, battery voltage as text)
            ac_status = "up" if self.ac_power_ok else "down"
            voltage = f"{self.battery_voltage:.2f}V"
            self.push("AC (Kuma)", self.ac_webhook,
                      {"status": ac_status, "message": voltage})
            self.push("AC (Tianji)", self.tianji_ac_webhook,
                      {"status": ac_status, "msg": voltage})

            # Device health (battery capacity as the value)
            capacity = f"{self.battery_capacity:.0f}"
            self.push("Device (Kuma)", self.device_webhook, {"ping": capacity})
            self.push("Device (Tianji)", self.tianji_device_webhook,
                      {"status": "up", "msg": f"{capacity}%", "value": capacity})
            
    
    def signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully."""
        self.log(f"Received signal {signum}, shutting down...")
        self.running = False
    
    def cleanup(self):
        """Clean up GPIO and I2C resources."""
        self.log("Cleaning up resources...")
        try:
            if hasattr(self, 'output_request'):
                self.output_request.set_value(self.pin_buzzer, Value.INACTIVE)
                self.output_request.set_value(self.pin_button, Value.INACTIVE)
                self.output_request.release()
            if hasattr(self, 'shutdown_request'):
                self.shutdown_request.release()
            if hasattr(self, 'pld_request'):
                self.pld_request.release()
            if hasattr(self, 'i2c') and self.i2c:
                self.i2c.close()
        except Exception as e:
            self.log(f"ERROR during cleanup: {e}")
    
    def run(self):
        """Start all monitoring threads and run service."""
        self.log("Starting X728 service...")
        
        # Start all threads
        threads = [
            threading.Thread(target=self.battery_monitor_thread, daemon=True),
            threading.Thread(target=self.button_monitor_thread, daemon=True),
            threading.Thread(target=self.power_monitor_thread, daemon=True),
            threading.Thread(target=self.watchdog_thread, daemon=True)
        ]
        
        for thread in threads:
            thread.start()
        
        self.log("All threads started, service running")
        
        # Keep main thread alive
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            self.log("Interrupted by user")
        finally:
            self.cleanup()
            self.log("Service stopped")


if __name__ == "__main__":
    service = X728Service()
    service.run()


