#!/usr/bin/env python3
"""
LED test script — run this on the Pi to isolate hardware vs software issues.

It tries three approaches in order:
  1. aiy.board (what the main app uses)
  2. gpiozero LED on pin 25 (AIY Voice Hat LED pin)
  3. RPi.GPIO raw output on pin 25

Each approach prints what it tried and whether it succeeded.
"""
import time
import sys

BLINK_COUNT = 5
BLINK_ON = 0.5
BLINK_OFF = 0.5

# AIY Voice Hat wires the button LED to BCM pin 25.
# Change this if your LED is wired to a different pin.
LED_PIN = 25


def try_aiy():
    print("\n--- Approach 1: aiy.board ---")
    try:
        from aiy.board import Board, Led
    except ImportError as e:
        print(f"  SKIP: aiy.board not importable — {e}")
        return False

    try:
        with Board() as board:
            print(f"  Board opened OK. Turning LED ON for 2s...")
            board.led.state = Led.ON
            time.sleep(2)
            print(f"  Blinking {BLINK_COUNT} times...")
            board.led.state = Led.BLINK
            time.sleep(BLINK_COUNT * (BLINK_ON + BLINK_OFF))
            board.led.state = Led.OFF
            print("  Done. LED OFF.")
        return True
    except Exception as e:
        print(f"  FAIL: {e}")
        return False


def try_gpiozero():
    print("\n--- Approach 2: gpiozero ---")
    try:
        from gpiozero import LED
    except ImportError as e:
        print(f"  SKIP: gpiozero not importable — {e}")
        return False

    try:
        led = LED(LED_PIN)
        print(f"  gpiozero LED on BCM pin {LED_PIN}. Turning ON for 2s...")
        led.on()
        time.sleep(2)
        print(f"  Blinking {BLINK_COUNT} times...")
        for _ in range(BLINK_COUNT):
            led.on()
            time.sleep(BLINK_ON)
            led.off()
            time.sleep(BLINK_OFF)
        led.close()
        print("  Done. LED OFF.")
        return True
    except Exception as e:
        print(f"  FAIL: {e}")
        return False


def try_rpigpio():
    print("\n--- Approach 3: RPi.GPIO raw ---")
    try:
        import RPi.GPIO as GPIO
    except ImportError as e:
        print(f"  SKIP: RPi.GPIO not importable — {e}")
        return False

    try:
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(LED_PIN, GPIO.OUT)
        print(f"  RPi.GPIO BCM pin {LED_PIN} set as OUTPUT. Turning ON for 2s...")
        GPIO.output(LED_PIN, GPIO.HIGH)
        time.sleep(2)
        print(f"  Blinking {BLINK_COUNT} times...")
        for _ in range(BLINK_COUNT):
            GPIO.output(LED_PIN, GPIO.HIGH)
            time.sleep(BLINK_ON)
            GPIO.output(LED_PIN, GPIO.LOW)
            time.sleep(BLINK_OFF)
        GPIO.output(LED_PIN, GPIO.LOW)
        GPIO.cleanup()
        print("  Done. LED OFF.")
        return True
    except Exception as e:
        print(f"  FAIL: {e}")
        GPIO.cleanup()
        return False


if __name__ == "__main__":
    print("=== Pi LED Test ===")
    print(f"Python: {sys.version}")
    print(f"Target LED pin: BCM {LED_PIN}")

    results = {
        "aiy.board": try_aiy(),
        "gpiozero": try_gpiozero(),
        "RPi.GPIO": try_rpigpio(),
    }

    print("\n=== Summary ===")
    for name, ok in results.items():
        status = "PASS" if ok else "FAIL/SKIP"
        print(f"  {name}: {status}")

    if not any(results.values()):
        print("\nAll approaches failed. Possible causes:")
        print("  - Not running on a Raspberry Pi")
        print("  - LED wired to a different GPIO pin (check your hat's schematic)")
        print("  - Missing library: pip install gpiozero RPi.GPIO")
        print("  - Need sudo: try  sudo python3 test_led.py")
        print("  - GPIO chip requires a different backend (Pi 5 needs lgpio)")
        sys.exit(1)
