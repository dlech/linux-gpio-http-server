from __future__ import annotations

import argparse
import logging
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, TypedDict, cast

import gpiod
import pyudev
from flask import Flask, jsonify, request
from gpiod.line import Direction, Value
from ruamel.yaml import YAML

LOGGER = logging.getLogger(__name__)


class PinConfig(TypedDict):
    name: str
    pin: int | str


class ChipConfig(TypedDict):
    name: str
    match: dict[str, str]
    pins: list[PinConfig]


class ServerConfig(TypedDict):
    chips: list[ChipConfig]


def _load_config(path: Path) -> ServerConfig:
    LOGGER.debug("Parsing config file %s", path)
    yaml = YAML(typ="safe")

    with path.open("r", encoding="utf-8") as file_obj:
        loaded: object = yaml.load(file_obj)  # pyright: ignore[reportUnknownMemberType]

    if not isinstance(loaded, dict):
        raise ValueError("config must be a mapping")

    data = cast(dict[str, object], loaded)

    chips_raw_obj = data.get("chips")

    if not isinstance(chips_raw_obj, list):
        raise ValueError("config must contain a top-level 'chips' list")

    chips_raw = cast(list[object], chips_raw_obj)

    chips: list[ChipConfig] = []

    for item_obj in chips_raw:
        if not isinstance(item_obj, dict):
            raise ValueError("each chip entry must be a mapping")

        item = cast(dict[str, object], item_obj)

        chip_name = item.get("name")

        if not isinstance(chip_name, str):
            raise ValueError("each chip must have string 'name'")

        match_raw = item.get("match")

        if not isinstance(match_raw, dict):
            raise ValueError("each chip must have 'match' mapping of string keys/values")

        match_items = cast(dict[object, object], match_raw)
        match: dict[str, str] = {}

        for key, value in match_items.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("each chip must have 'match' mapping of string keys/values")

            match[key] = value

        pins_raw_obj = item.get("pins")

        if not isinstance(pins_raw_obj, list):
            raise ValueError("each chip must have 'pins' list")

        pins_raw = cast(list[object], pins_raw_obj)

        pins: list[PinConfig] = []

        for pin_obj in pins_raw:
            if not isinstance(pin_obj, dict):
                raise ValueError("each pin entry must be a mapping")

            pin = cast(dict[str, object], pin_obj)

            pin_name = pin.get("name")
            pin_id = pin.get("pin")

            if not isinstance(pin_name, str):
                raise ValueError("each pin must have string 'name'")

            if not isinstance(pin_id, int | str):
                raise ValueError("each pin must have 'pin' as int offset or string line name")

            pins.append(PinConfig(name=pin_name, pin=pin_id))

        chips.append(ChipConfig(name=chip_name, match=match, pins=pins))

    LOGGER.debug("Loaded config with %d chip definitions", len(chips))
    return ServerConfig(chips=chips)


def _matches_ancestors(device: pyudev.Device, match: Mapping[str, str]) -> bool:
    current: pyudev.Device | None = device

    LOGGER.debug("Looking for match=%s in device %s and its ancestors", match, device.device_path)

    while current is not None:
        if all(str(current.properties.get(key)) == str(value) for key, value in match.items()):
            LOGGER.debug("Match found at udev device %s", current.device_path)
            return True

        current = current.parent

    return False


def _find_matching_gpiochip(
    context: pyudev.Context, match: Mapping[str, str]
) -> pyudev.Device | None:
    for device in context.list_devices(subsystem="gpio"):
        if not device.device_node or not device.device_node.startswith("/dev/gpiochip"):
            continue

        LOGGER.debug("Evaluating gpio candidate %s", device.device_node)

        if _matches_ancestors(device, match):
            return device

    return None


def _parse_on_off(raw: str) -> bool:
    value = raw.strip().lower()
    LOGGER.debug("Parsing request body value %r", raw)

    if value == "on":
        return True

    if value == "off":
        return False

    raise ValueError("request body must be exactly 'On' or 'Off'")


def main() -> None:
    parser = argparse.ArgumentParser(description="HTTP server for Linux GPIO chips")
    parser.add_argument("config", help="Path to YAML config file")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to server")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind to server")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    LOGGER.info("Loading configuration from %s", args.config)

    config = _load_config(Path(args.config))
    context = pyudev.Context()

    chip_devpaths: dict[str, str] = {}
    chip_nodes: dict[str, str] = {}

    chips = config["chips"]
    LOGGER.info("Finding %d gpiochip(s) via udev", len(chips))

    for item in chips:
        chip_name = item["name"]
        match = item["match"]

        device = _find_matching_gpiochip(context, match)

        if device is None:
            LOGGER.error("No gpiochip found for '%s' with match=%s", chip_name, match)
            raise SystemExit(1)

        chip_devpaths[chip_name] = str(device.properties.get("DEVPATH") or device.device_path)
        chip_nodes[chip_name] = str(device.device_node)
        LOGGER.info(
            "Matched chip '%s' to node=%s devpath=%s",
            chip_name,
            chip_nodes[chip_name],
            chip_devpaths[chip_name],
        )

    pin_offsets: dict[str, dict[str, int]] = {}
    pin_request_ids: dict[str, dict[str, int | str]] = {}
    requests_by_chip: dict[str, gpiod.LineRequest] = {}

    for item in chips:
        chip_name = item["name"]
        pin_offsets[chip_name] = {}
        pin_request_ids[chip_name] = {}

        chip = gpiod.Chip(chip_nodes[chip_name])
        LOGGER.info("Requesting lines for chip '%s'", chip_name)

        line_config: dict[Iterable[int | str] | int | str, gpiod.LineSettings | None] = {}

        for pin in item["pins"]:
            pin_name = pin["name"]
            pin_id = pin["pin"]

            offset = pin_id if isinstance(pin_id, int) else chip.line_offset_from_id(pin_id)
            pin_offsets[chip_name][pin_name] = int(offset)
            pin_request_ids[chip_name][pin_name] = pin_id
            line_config[pin_id] = gpiod.LineSettings(direction=Direction.OUTPUT)
            LOGGER.info(
                "Configured chip='%s' pin='%s' id=%s offset=%d as output",
                chip_name,
                pin_name,
                pin_id,
                int(offset),
            )

        requests_by_chip[chip_name] = chip.request_lines(
            config=line_config,
            consumer="linux-gpio-http-server",
        )

    app = Flask(__name__)

    @app.get("/")
    def index() -> Any:  # pyright: ignore[reportUnusedFunction]
        return jsonify(
            {
                "chips": {
                    chip_name: {
                        "devpath": chip_devpaths[chip_name],
                        "pins": pin_offsets[chip_name],
                    }
                    for chip_name in chip_devpaths
                }
            }
        )

    @app.route("/<chip_name>/<pin_name>", methods=["GET", "PUT"])
    def chip_pin(chip_name: str, pin_name: str) -> Any:  # pyright: ignore[reportUnusedFunction]

        if chip_name not in requests_by_chip:
            LOGGER.warning("Request for unknown chip '%s'", chip_name)
            return jsonify({"error": f"unknown chip '{chip_name}'"}), 404

        if pin_name not in pin_request_ids[chip_name]:
            LOGGER.warning("Request for unknown pin '%s' on chip '%s'", pin_name, chip_name)
            return jsonify({"error": f"unknown pin '{pin_name}' for chip '{chip_name}'"}), 404

        line_request = requests_by_chip[chip_name]
        line_id = pin_request_ids[chip_name][pin_name]

        if request.method == "GET":
            value = line_request.get_value(line_id)
            LOGGER.debug("Read chip='%s' pin='%s' value=%s", chip_name, pin_name, value)
            return "On" if value == Value.ACTIVE else "Off"

        raw_body = request.get_data(as_text=True)
        LOGGER.debug("PUT body for chip='%s' pin='%s': %r", chip_name, pin_name, raw_body)

        try:
            bool_value = _parse_on_off(raw_body)

        except ValueError as ex:
            LOGGER.warning(
                "Invalid body for chip='%s' pin='%s': %r",
                chip_name,
                pin_name,
                raw_body,
            )
            return jsonify({"error": str(ex)}), 400

        # Reconfigure on each write so a line that starts as input is switched to output
        # with the requested level atomically.
        line_request.reconfigure_lines(
            {
                line_id: gpiod.LineSettings(
                    direction=Direction.OUTPUT,
                    output_value=Value.ACTIVE if bool_value else Value.INACTIVE,
                )
            }
        )
        LOGGER.info(
            "Set chip='%s' pin='%s' to %s",
            chip_name,
            pin_name,
            "On" if bool_value else "Off",
        )

        value = line_request.get_value(line_id)
        LOGGER.debug("Read chip='%s' pin='%s' value=%s", chip_name, pin_name, value)
        return "On" if value == Value.ACTIVE else "Off"

    LOGGER.info("Starting HTTP server on %s:%d", args.host, args.port)
    app.run(host=args.host, port=args.port)
