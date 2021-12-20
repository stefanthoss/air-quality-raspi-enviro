#!/usr/bin/env python3

import argparse
import logging
import os
import subprocess
import time
from threading import Thread

import aqi
import requests
import ST7735
from bme280 import BME280
from enviroplus import gas
from fonts.ttf import RobotoMedium as UserFont
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
from PIL import Image, ImageDraw, ImageFont
from pms5003 import PMS5003
from pms5003 import ReadTimeoutError as pmsReadTimeoutError
from prometheus_client import Gauge, Histogram, start_http_server

try:
    from smbus2 import SMBus
except ImportError:
    from smbus import SMBus

try:
    # Transitional fix for breaking change in LTR559
    from ltr559 import LTR559

    ltr559 = LTR559()
except ImportError:
    import ltr559

logging.basicConfig(
    format="%(asctime)s.%(msecs)03d %(levelname)-8s %(message)s",
    level=logging.INFO,
    handlers=[logging.FileHandler("enviroplus_exporter.log"), logging.StreamHandler()],
    datefmt="%Y-%m-%d %H:%M:%S",
)

logging.info(
    """enviroplus_exporter.py - Expose readings from the Enviro+ sensor by Pimoroni in Prometheus format

Press Ctrl+C to exit!

"""
)

AQI_CATEGORIES = {
    (-1, 50): "Good",
    (50, 100): "Moderate",
    (100, 150): "Unhealthy for Sensitive Groups",
    (150, 200): "Unhealthy",
    (200, 300): "Very Unhealthy",
    (300, 500): "Hazardous",
}

AQI_COLORS = {
    (-1, 50): (0, 128, 0),
    (50, 100): (255, 255, 0),
    (100, 150): (255, 165, 0),
    (150, 200): (255, 0, 0),
    (200, 300): (128, 0, 128),
    (300, 500): (128, 0, 0),
}

DEBUG = os.getenv("DEBUG", "false") == "true"

bus = SMBus(1)
bme280 = BME280(i2c_dev=bus)
pms5003 = PMS5003()

# Create ST7735 LCD display class
disp = ST7735.ST7735(port=0, cs=1, dc=9, backlight=12, rotation=270, spi_speed_hz=10000000)
disp.begin()

# Set up canvas and font
img = Image.new("RGB", (disp.width, disp.height), color=(0, 0, 0))
draw = ImageDraw.Draw(img)
font_size = 48
font = ImageFont.truetype(UserFont, font_size)
DISPLAY_TIME_BETWEEN_UPDATES = 10

TEMPERATURE = Gauge("temperature", "Temperature measured (*C)")
PRESSURE = Gauge("pressure", "Pressure measured (hPa)")
HUMIDITY = Gauge("humidity", "Relative humidity measured (%)")
OXIDISING = Gauge("oxidising", "Mostly nitrogen dioxide but could include NO and Hydrogen (Ohms)")
REDUCING = Gauge(
    "reducing",
    "Mostly carbon monoxide but could include H2S, Ammonia, Ethanol, Hydrogen, Methane, Propane, Iso-butane (Ohms)",
)
NH3 = Gauge("NH3", "mostly Ammonia but could also include Hydrogen, Ethanol, Propane, Iso-butane (Ohms)")
LUX = Gauge("lux", "current ambient light level (lux)")
PROXIMITY = Gauge("proximity", "proximity, with larger numbers being closer proximity and vice versa")
PM1 = Gauge("PM1", "Particulate Matter of diameter less than 1 micron. Measured in micrograms per cubic metre (ug/m3)")
PM25 = Gauge(
    "PM25", "Particulate Matter of diameter less than 2.5 microns. Measured in micrograms per cubic metre (ug/m3)"
)
PM10 = Gauge(
    "PM10", "Particulate Matter of diameter less than 10 microns. Measured in micrograms per cubic metre (ug/m3)"
)
AQI = Gauge("AQI", "AQI value based on PM2.5 and PM10 pollutant concentration using the EPA algorithm")

OXIDISING_HIST = Histogram(
    "oxidising_measurements",
    "Histogram of oxidising measurements",
    buckets=(
        0,
        10000,
        15000,
        20000,
        25000,
        30000,
        35000,
        40000,
        45000,
        50000,
        55000,
        60000,
        65000,
        70000,
        75000,
        80000,
        85000,
        90000,
        100000,
    ),
)
REDUCING_HIST = Histogram(
    "reducing_measurements",
    "Histogram of reducing measurements",
    buckets=(
        0,
        100000,
        200000,
        300000,
        400000,
        500000,
        600000,
        700000,
        800000,
        900000,
        1000000,
        1100000,
        1200000,
        1300000,
        1400000,
        1500000,
    ),
)
NH3_HIST = Histogram(
    "nh3_measurements",
    "Histogram of nh3 measurements",
    buckets=(
        0,
        10000,
        110000,
        210000,
        310000,
        410000,
        510000,
        610000,
        710000,
        810000,
        910000,
        1010000,
        1110000,
        1210000,
        1310000,
        1410000,
        1510000,
        1610000,
        1710000,
        1810000,
        1910000,
        2000000,
    ),
)

PM1_HIST = Histogram(
    "pm1_measurements",
    "Histogram of Particulate Matter of diameter less than 1 micron measurements",
    buckets=(0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 100),
)
PM25_HIST = Histogram(
    "pm25_measurements",
    "Histogram of Particulate Matter of diameter less than 2.5 micron measurements",
    buckets=(0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 100),
)
PM10_HIST = Histogram(
    "pm10_measurements",
    "Histogram of Particulate Matter of diameter less than 10 micron measurements",
    buckets=(0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 100),
)

# Setup InfluxDB
# You can generate an InfluxDB Token from the Tokens Tab in the InfluxDB Cloud UI
INFLUXDB_URL = os.getenv("INFLUXDB_URL", "")
INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN", "")
INFLUXDB_ORG_ID = os.getenv("INFLUXDB_ORG_ID", "")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET", "sensors")
INFLUXDB_TIME_BETWEEN_POSTS = int(os.getenv("INFLUXDB_TIME_BETWEEN_POSTS", "5"))
INFLUXDB_MEASUREMENT = os.getenv("INFLUXDB_MEASUREMENT", "air_quality")
influxdb_client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG_ID)
influxdb_api = influxdb_client.write_api(write_options=SYNCHRONOUS)

# Setup Luftdaten
LUFTDATEN_TIME_BETWEEN_POSTS = int(os.getenv("LUFTDATEN_TIME_BETWEEN_POSTS", "30"))


# Sometimes the sensors can't be read. Resetting the i2c
def reset_i2c():
    subprocess.run(["i2cdetect", "-y", "1"])
    time.sleep(2)


# Displays data and text on the 0.96" LCD
def display_text(message, text_color):
    # Draw a black filled box to clear the image
    draw.rectangle((0, 0, disp.width, disp.height), (0, 0, 0))
    disp.display(img)

    # Write the text
    (font_width, font_height) = font.getsize(message)
    draw.text(
        (disp.width // 2 - font_width // 2, disp.height // 2 - font_height // 2),
        message,
        font=font,
        fill=text_color,
    )
    disp.display(img)


def get_aqi_category(aqi_value):
    for limits, category in AQI_CATEGORIES.items():
        if aqi_value > limits[0] and aqi_value <= limits[1]:
            return category


def get_aqi_color(aqi_value):
    for limits, color in AQI_COLORS.items():
        if aqi_value > limits[0] and aqi_value <= limits[1]:
            return color


# Get the temperature of the CPU for compensation
def get_cpu_temperature():
    with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
        temp = f.read()
        temp = int(temp) / 1000.0
    return temp


def get_temperature(factor):
    """Get temperature from the weather sensor"""
    # Tuning factor for compensation. Decrease this number to adjust the
    # temperature down, and increase to adjust up
    raw_temp = bme280.get_temperature()

    if factor:
        cpu_temps = [get_cpu_temperature()] * 5
        cpu_temp = get_cpu_temperature()
        # Smooth out with some averaging to decrease jitter
        cpu_temps = cpu_temps[1:] + [cpu_temp]
        avg_cpu_temp = sum(cpu_temps) / float(len(cpu_temps))
        temperature = raw_temp - ((avg_cpu_temp - raw_temp) / factor)
    else:
        temperature = raw_temp

    TEMPERATURE.set(temperature)  # Set to a given value


def get_pressure():
    """Get pressure from the weather sensor"""
    try:
        pressure = bme280.get_pressure()
        PRESSURE.set(pressure)
    except IOError:
        logging.error("Could not get pressure readings. Resetting i2c.")
        reset_i2c()


def get_humidity():
    """Get humidity from the weather sensor"""
    try:
        humidity = bme280.get_humidity()
        HUMIDITY.set(humidity)
    except IOError:
        logging.error("Could not get humidity readings. Resetting i2c.")
        reset_i2c()


def get_gas():
    """Get all gas readings"""
    try:
        readings = gas.read_all()

        OXIDISING.set(readings.oxidising)
        OXIDISING_HIST.observe(readings.oxidising)

        REDUCING.set(readings.reducing)
        REDUCING_HIST.observe(readings.reducing)

        NH3.set(readings.nh3)
        NH3_HIST.observe(readings.nh3)
    except IOError:
        logging.error("Could not get gas readings. Resetting i2c.")
        reset_i2c()


def get_light():
    """Get all light readings"""
    try:
        lux = ltr559.get_lux()
        prox = ltr559.get_proximity()

        LUX.set(lux)
        PROXIMITY.set(prox)
    except IOError:
        logging.error("Could not get lux and proximity readings. Resetting i2c.")
        reset_i2c()


def get_particulates():
    """Get the particulate matter readings"""
    try:
        pms_data = pms5003.read()
    except pmsReadTimeoutError:
        logging.warning("Failed to read PMS5003")
    except IOError:
        logging.error("Could not get particulate matter readings. Resetting i2c.")
        reset_i2c()
    else:
        PM1.set(pms_data.pm_ug_per_m3(1.0))
        PM25.set(pms_data.pm_ug_per_m3(2.5))
        PM10.set(pms_data.pm_ug_per_m3(10))

        # Workaround for https://github.com/hrbonz/python-aqi/issues/27
        pm25_value = 500.4 if pms_data.pm_ug_per_m3(2.5) > 500.4 else pms_data.pm_ug_per_m3(2.5)
        pm10_value = 604 if pms_data.pm_ug_per_m3(10) > 604 else pms_data.pm_ug_per_m3(10)
        AQI.set(aqi.to_aqi([(aqi.POLLUTANT_PM25, pm25_value), (aqi.POLLUTANT_PM10, pm10_value)]))

        PM1_HIST.observe(pms_data.pm_ug_per_m3(1.0))
        PM25_HIST.observe(pms_data.pm_ug_per_m3(2.5) - pms_data.pm_ug_per_m3(1.0))
        PM10_HIST.observe(pms_data.pm_ug_per_m3(10) - pms_data.pm_ug_per_m3(2.5))


def collect_all_data():
    """Collects all the data currently set"""
    sensor_data = {}
    sensor_data["BME280_temperature"] = TEMPERATURE.collect()[0].samples[0].value
    sensor_data["BME280_humidity"] = HUMIDITY.collect()[0].samples[0].value
    sensor_data["BME280_pressure"] = PRESSURE.collect()[0].samples[0].value
    sensor_data["MICS6814_oxidising"] = OXIDISING.collect()[0].samples[0].value
    sensor_data["MICS6814_reducing"] = REDUCING.collect()[0].samples[0].value
    sensor_data["MICS6814_nh3"] = NH3.collect()[0].samples[0].value
    sensor_data["LTR559_lux"] = LUX.collect()[0].samples[0].value
    sensor_data["LTR559_proximity"] = PROXIMITY.collect()[0].samples[0].value
    sensor_data["PMS_P0"] = PM1.collect()[0].samples[0].value
    sensor_data["PMS_P1"] = PM10.collect()[0].samples[0].value
    sensor_data["PMS_P2"] = PM25.collect()[0].samples[0].value
    sensor_data["AQI_value"] = AQI.collect()[0].samples[0].value
    sensor_data["AQI_category"] = get_aqi_category(AQI.collect()[0].samples[0].value)
    return sensor_data


def refresh_display():
    """Refresh AQI value on display"""
    display_text("Start", (255, 255, 255))

    previous_aqi = -1
    while True:
        time.sleep(DISPLAY_TIME_BETWEEN_UPDATES)
        sensor_data = collect_all_data()
        if int(sensor_data["AQI_value"]) != previous_aqi:
            previous_aqi = int(sensor_data["AQI_value"])
            display_text("{}".format(previous_aqi), get_aqi_color(previous_aqi))


def post_to_influxdb():
    """Post all sensor data to InfluxDB"""
    while True:
        time.sleep(INFLUXDB_TIME_BETWEEN_POSTS)
        data_points = []
        sensor_data = collect_all_data()
        for field_name in sensor_data:
            data_points.append(
                Point(INFLUXDB_MEASUREMENT).tag("node", SENSOR_UID).field(field_name, sensor_data[field_name])
            )
        try:
            influxdb_api.write(bucket=INFLUXDB_BUCKET, record=data_points)
            if DEBUG:
                logging.info("InfluxDB response: OK")
        except Exception as exception:
            logging.warning("Exception sending to InfluxDB: {}".format(exception))


def post_to_luftdaten():
    """Post relevant sensor data to luftdaten.info"""
    """Code from: https://github.com/sepulworld/balena-environ-plus"""
    while True:
        time.sleep(LUFTDATEN_TIME_BETWEEN_POSTS)
        sensor_data = collect_all_data()
        values = {}
        values["P2"] = sensor_data["pm25"]
        values["P1"] = sensor_data["pm10"]
        values["P0"] = sensor_data["pm1"]
        values["temperature"] = "{:.2f}".format(sensor_data["temperature"])
        values["pressure"] = "{:.2f}".format(sensor_data["pressure"] * 100)
        values["humidity"] = "{:.2f}".format(sensor_data["humidity"])
        pm_values = dict(i for i in values.items() if i[0].startswith("P"))
        temperature_values = dict(i for i in values.items() if not i[0].startswith("P"))
        try:
            response_pin_1 = requests.post(
                "https://api.luftdaten.info/v1/push-sensor-data/",
                json={
                    "software_version": "enviro-plus 0.0.1",
                    "sensordatavalues": [{"value_type": key, "value": val} for key, val in pm_values.items()],
                },
                headers={
                    "X-PIN": "1",
                    "X-Sensor": SENSOR_UID,
                    "Content-Type": "application/json",
                    "cache-control": "no-cache",
                },
            )

            response_pin_11 = requests.post(
                "https://api.luftdaten.info/v1/push-sensor-data/",
                json={
                    "software_version": "enviro-plus 0.0.1",
                    "sensordatavalues": [{"value_type": key, "value": val} for key, val in temperature_values.items()],
                },
                headers={
                    "X-PIN": "11",
                    "X-Sensor": SENSOR_UID,
                    "Content-Type": "application/json",
                    "cache-control": "no-cache",
                },
            )

            if response_pin_1.ok and response_pin_11.ok:
                if DEBUG:
                    logging.info("Luftdaten response: OK")
            else:
                logging.warning("Luftdaten response: Failed")
        except Exception as exception:
            logging.warning("Exception sending to Luftdaten: {}".format(exception))


def get_serial_number():
    """Get Raspberry Pi serial number to use as SENSOR_UID"""
    with open("/proc/cpuinfo", "r") as f:
        for line in f:
            if line[0:6] == "Serial":
                return str(line.split(":")[1].strip())


def str_to_bool(value):
    if value.lower() in {"false", "f", "0", "no", "n"}:
        return False
    elif value.lower() in {"true", "t", "1", "yes", "y"}:
        return True
    raise ValueError("{} is not a valid boolean value".format(value))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-b", "--bind", metavar="ADDRESS", default="0.0.0.0", help="Specify alternate bind address [default: 0.0.0.0]"
    )
    parser.add_argument(
        "-p", "--port", metavar="PORT", default=8000, type=int, help="Specify alternate port [default: 8000]"
    )
    parser.add_argument(
        "-f",
        "--factor",
        metavar="FACTOR",
        type=float,
        help="The compensation factor to get better temperature results when the Enviro+ pHAT is too close to the Raspberry Pi board",
    )
    parser.add_argument(
        "-e",
        "--enviro",
        metavar="ENVIRO",
        type=str_to_bool,
        help="Device is an Enviro (not Enviro+) so don't fetch data from gas and particulate sensors as they don't exist",
    )
    parser.add_argument(
        "-d",
        "--debug",
        metavar="DEBUG",
        type=str_to_bool,
        help="Turns on more verbose logging, showing sensor output and post responses [default: false]",
    )
    parser.add_argument(
        "-i",
        "--influxdb",
        metavar="INFLUXDB",
        type=str_to_bool,
        default="false",
        help="Post sensor data to InfluxDB [default: false]",
    )
    parser.add_argument(
        "-l",
        "--luftdaten",
        metavar="LUFTDATEN",
        type=str_to_bool,
        default="false",
        help="Post sensor data to Luftdaten [default: false]",
    )
    parser.add_argument(
        "-s",
        "--show",
        metavar="SHOW",
        type=str_to_bool,
        default="true",
        help="Show AQI value on display [default: true]",
    )
    args = parser.parse_args()

    # Start up the server to expose the metrics.
    start_http_server(addr=args.bind, port=args.port)

    SENSOR_UID = "raspi-" + get_serial_number()

    if args.debug:
        DEBUG = True

    if args.factor:
        logging.info(
            "Using compensating algorithm (factor={}) to account for heat leakage from Raspberry Pi board".format(
                args.factor
            )
        )

    if args.influxdb:
        # Post to InfluxDB in another thread
        logging.info(
            "Sensor data will be posted to InfluxDB every {} seconds for the node {}".format(
                INFLUXDB_TIME_BETWEEN_POSTS, SENSOR_UID
            )
        )
        influx_thread = Thread(target=post_to_influxdb)
        influx_thread.start()

    if args.luftdaten:
        # Post to Luftdaten in another thread
        logging.info(
            "Sensor data will be posted to Luftdaten every {} seconds for the UID {}".format(
                LUFTDATEN_TIME_BETWEEN_POSTS, SENSOR_UID
            )
        )
        luftdaten_thread = Thread(target=post_to_luftdaten)
        luftdaten_thread.start()

    if args.show:
        # Refresh display in another thread
        logging.info(
            "Display will be refreshed every {} seconds for the UID {}".format(DISPLAY_TIME_BETWEEN_UPDATES, SENSOR_UID)
        )
        display_thread = Thread(target=refresh_display)
        display_thread.start()

    logging.info("Listening on http://{}:{}".format(args.bind, args.port))

    while True:
        get_temperature(args.factor)
        get_pressure()
        get_humidity()
        get_light()
        if not args.enviro:
            get_gas()
            get_particulates()
        if DEBUG:
            logging.info("Sensor data: {}".format(collect_all_data()))
