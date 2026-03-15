import RPi.GPIO as GPIO
import time

RELAY_PIN = 17

GPIO.setmode(GPIO.BCM)
GPIO.setup(RELAY_PIN, GPIO.OUT)

try:
    while True:
        # bật relay (active LOW)
        GPIO.output(RELAY_PIN, GPIO.LOW)
        print("Relay ON")
        time.sleep(1)

        # tắt relay
        GPIO.output(RELAY_PIN, GPIO.HIGH)
        print("Relay OFF")
        time.sleep(1)

except KeyboardInterrupt:
    GPIO.cleanup()