import asyncio
import time
import logging
from yeelight import Bulb, PowerMode

from app.ingternal.device.schemas.config import ChangeField, ConfigSchema
from app.ingternal.device.schemas.device import DeviceSerializeSchema, DeviceInitFieldSchema
from app.ingternal.device.classes.baseDevice import BaseDevice
from app.ingternal.device.schemas.enums import TypeDeviceField, DeviceGetData
from app.ingternal.device.interface.field_class import IField
from app.ingternal.device_types.types_names import TypesDeviceEnum
from app.ingternal.logs.logs import LogManager


logger = logging.getLogger(__name__)
logsHandler = LogManager("YeelightDevice", level=logging.DEBUG)
logger.addHandler(logsHandler.get_file_handler())
logger.setLevel(logging.DEBUG)


class YeelightDevice(BaseDevice):

	device_config = ConfigSchema(
		class_img="Yeelight/unnamed.jpg",
		fields_creation=False,
		init_field=True,
		virtual=False,
		token=False,
		type_get_data=False,
		type=True,
		available_types=[TypesDeviceEnum.LIGHT],
		fields_change=ChangeField(
			creation=False,
			deleted=False,
			address=False,
			control=False,
			read_only=False,
			virtual_field=False,
			high=False,
			low=False,
			type=False,
			unit=False,
			name=False,
			enum_values=False,
		),
	)

	# -------------------- INIT --------------------

	def __init__(self, device: DeviceSerializeSchema):
		super().__init__(device)

		self.device: Bulb | None = None
		self.cached_values: dict = {}

		self._lock = asyncio.Lock()
		self._initialized = False
		self._last_poll = 0.0
		self._poll_interval = 5.0  # сек

		if not self.data.address:
			logger.warning("Device address is missing.")
			return

		self.device = Bulb(self.data.address)
		self.data.type_get_data = DeviceGetData.PULL

	# -------------------- INTERNAL --------------------

	async def _call(self, fn, *args):
		"""Run blocking Yeelight call in executor"""
		loop = asyncio.get_running_loop()
		return await loop.run_in_executor(None, fn, *args)

	# -------------------- INIT DEVICE --------------------

	async def async_init(self):
		if self._initialized or not self.device:
			return

		async with self._lock:
			try:
				values = await self._call(self.device.get_properties)
				if not values:
					logger.warning("Failed to retrieve device properties.")
					return

				self.cached_values = values

				# model specs — один раз
				try:
					self.minmaxValue = await self._call(self.device.get_model_specs)
				except Exception:
					self.minmaxValue = {
						"color_temp": {"min": 1700, "max": 6500},
						"night_light": True,
					}

				# night light
				if (
					self.get_field_by_name("night_light") is None
					and self.minmaxValue.get("night_light")
				):
					self._add_field(DeviceInitFieldSchema(
						name="night_light",
						read_only=False,
						high="1",
						low="0",
						type=TypeDeviceField.BINARY,
						icon="",
						value=values.get("active_mode", 0),
						virtual_field=False,
					))

				field_mappings = {
					"state": ("power", "1", "0", TypeDeviceField.BINARY),
					"bg_power": ("bg_power", "1", "0", TypeDeviceField.BINARY),
					"brightness": ("current_brightness", "100", "0", TypeDeviceField.NUMBER),
					"bg_bright": ("bg_bright", "100", "0", TypeDeviceField.NUMBER),
					"color": ("hue", "360", "0", TypeDeviceField.NUMBER),
					"bg_color": ("bg_hue", "360", "0", TypeDeviceField.NUMBER),
					"saturation": ("sat", "100", "0", TypeDeviceField.NUMBER),
					"bg_saturation": ("bg_sat", "100", "0", TypeDeviceField.NUMBER),
					"temp": (
						"ct",
						str(self.minmaxValue["color_temp"]["max"]),
						str(self.minmaxValue["color_temp"]["min"]),
						TypeDeviceField.NUMBER,
					),
					"bg_temp": ("bg_ct", "6500", "1700", TypeDeviceField.NUMBER),
				}

				for field, (key, high, low, field_type) in field_mappings.items():
					if self.get_field_by_name(field) is None and values.get(key) is not None:
						self._add_field(DeviceInitFieldSchema(
							name=field,
							read_only=False,
							high=high,
							low=low,
							type=field_type,
							icon="",
							value=values[key],
							virtual_field=False,
						))

				self._initialized = True
				logger.debug("Yeelight initialized successfully")

			except Exception:
				logger.exception("Yeelight initialization error")

	# -------------------- STATE --------------------

	@property
	def is_conected(self) -> bool:
		return self.device is not None and self._initialized

	# -------------------- POLLING --------------------

	async def async_load(self) -> dict[str, str]:
		if not self.device:
			return {}

		now = time.monotonic()
		if now - self._last_poll < self._poll_interval:
			return {}

		async with self._lock:
			try:
				values = await self._call(self.device.get_properties)
				self.cached_values = values
				self._last_poll = now
			except Exception as e:
				logger.warning(f"Yeelight poll failed: {e}")
				return {}

		patch: dict[str, str] = {}

		def maybe(name: str, new_val):
			field = self.get_field_by_name(name)
			if field and field.get() != new_val:
				field.set(new_val)
				patch[name] = new_val

		v = self.cached_values

		if "power" in v:
			maybe("state", "1" if v["power"] == "on" else "0")
		if "current_brightness" in v:
			maybe("brightness", v["current_brightness"])
		if "bg_bright" in v:
			maybe("bg_bright", v["bg_bright"])
		if "active_mode" in v:
			maybe("night_light", v["active_mode"])
		if "ct" in v:
			maybe("temp", v["ct"])
		if "bg_ct" in v:
			maybe("bg_temp", v["bg_ct"])
		if "hue" in v:
			maybe("color", v["hue"])
		if "bg_hue" in v:
			maybe("bg_color", v["bg_hue"])
		if "bg_power" in v:
			maybe("bg_power", "1" if v["bg_power"] == "on" else "0")
		if "sat" in v:
			maybe("saturation", v["sat"])
		if "bg_sat" in v:
			maybe("bg_saturation", v["bg_sat"])

		return patch

	# -------------------- SET VALUE --------------------

	async def set_value(self, field_id: str, value: str, script: bool = False):
		field = self.get_field(field_id)
		name = field.get_name()

		await super().set_value(field_id, value, script)

		async with self._lock:
			try:
				if name == "state":
					await self._call(
						self.device.send_command,
						"set_power",
						["on" if int(value) == 1 else "off"],
					)
					self.cached_values["power"] = "on" if int(value) == 1 else "off"

				elif name == "brightness":
					await self._call(self.device.set_brightness, int(value))
					self.cached_values["current_brightness"] = int(value)

				elif name == "bg_bright":
					await self._call(self.device.send_command, "bg_set_bright", [int(value)])
					self.cached_values["bg_bright"] = int(value)

				elif name == "temp":
					await self._call(self.device.set_power_mode, PowerMode.NORMAL)
					await self._call(self.device.set_color_temp, int(value))
					self.cached_values["ct"] = int(value)

				elif name == "bg_temp":
					await self._call(self.device.send_command, "bg_set_ct_abx", [int(value)])
					self.cached_values["bg_ct"] = int(value)

				elif name == "night_light":
					mode = PowerMode.MOONLIGHT if int(value) == 1 else PowerMode.NORMAL
					await self._call(self.device.set_power_mode, mode)
					self.cached_values["active_mode"] = int(value)

				elif name == "color":
					await self._call(
						self.device.set_hsv,
						int(value),
						int(self.get_field_by_name("saturation").get()),
					)
					self.cached_values["hue"] = int(value)

				elif name == "bg_color":
					await self._call(
						self.device.send_command,
						"bg_set_hsv",
						[int(value), int(self.get_field_by_name("bg_saturation").get())],
					)
					self.cached_values["bg_hue"] = int(value)

				elif name == "saturation":
					await self._call(
						self.device.set_hsv,
						int(self.get_field_by_name("color").get()),
						int(value),
					)
					self.cached_values["sat"] = int(value)

				elif name == "bg_saturation":
					await self._call(
						self.device.send_command,
						"bg_set_hsv",
						[int(self.get_field_by_name("bg_color").get()), int(value)],
					)
					self.cached_values["bg_sat"] = int(value)

				elif name == "bg_power":
					await self._call(
						self.device.send_command,
						"bg_set_power",
						["on" if int(value) == 1 else "off"],
					)
					self.cached_values["bg_power"] = "on" if int(value) == 1 else "off"

			except Exception as e:
				logger.exception(f"Error setting Yeelight value ({name}): {e}")

	# -------------------- CLOSE --------------------

	def close(self):
		self.device = None
