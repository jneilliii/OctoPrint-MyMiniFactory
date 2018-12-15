# coding=utf-8
from __future__ import absolute_import
from uuid import getnode as get_mac
from octoprint.server import user_permission
from octoprint.util import RepeatedTimer
from octoprint.util import version
from octoprint.events import Events
from octoprint.filemanager.analysis import QueueEntry
from datetime import datetime
import requests
import flask
import json
import time
import octoprint.plugin

class MyMiniFactoryPlugin(octoprint.plugin.SettingsPlugin,
						  octoprint.plugin.EventHandlerPlugin,
						  octoprint.plugin.StartupPlugin,
						  octoprint.plugin.ShutdownPlugin,
						  octoprint.plugin.AssetPlugin,
						  octoprint.plugin.TemplatePlugin,
						  octoprint.plugin.SimpleApiPlugin,
						  octoprint.printer.PrinterCallback):

	def __init__(self):
		self._mqtt = None
		self._mqtt_connected = False
		self._mqtt_tls_set = False
		self._current_task_id = None
		self.mmf_status_updater = None
		self._current_action_code = "999"
		self._current_temp_hotend = 0
		self._current_temp_bed = 0
		self._mmf_print = False
		self._printer_status = {"000":"free",
								"100":"prepare",
								"101":"printing",
								"102":"paused",
								"103":"free",
								"104":"printing",
								"999":"offline"}

	def initialize(self):
		self._printer.register_callback(self)

	##~~ SettingsPlugin mixin

	def get_settings_defaults(self):
		return dict(
			supported_printers = [],
			printer_manufacturer = "Anet",
			printer_model = "anet-a8",
			printer_serial_number = "",
			printer_firmware_version = "",
			registration_complete = False,
			printer_token = "",
			client_name = "octoprint_myminifactory",
			client_key = "acGxgLJmvgTZU2RDZ3vQaiitxc5Bf6DDeHL1", #"b4943605-52b5-4d13-94ee-34eb983a813f"
			auto_start_print = True,
			mmf_print_complete = False,
			mmf_print_cancelled = False,
			bypass_bed_clear = False
		)

	def get_settings_version(self):
		return 1
		
	def on_settings_migrate(self, target, current=None):
		self._logger.debug("Settings migrate complete.")

	##~~ EventHandlerPlugin API

	def on_event(self, event, payload):
		if event == Events.PRINT_STARTED:
			self._current_action_code = "101"
			self._settings.set_boolean(["mmf_print_complete"],False)
			self._settings.set_boolean(["mmf_print_cancelled"],False)
			self._settings.save()
			if not self._mmf_print:
				self._current_task_id = None
		elif event == Events.PRINT_DONE:
			if self._mmf_print and not self._settings.get_boolean(["bypass_bed_clear"]): # Send message back to UI to confirm clearing of bed.
				self._settings.set_boolean(["mmf_print_complete"],True)
				self._settings.save()
				self._plugin_manager.send_plugin_message(self._identifier, dict(mmf_print_complete=True))
			else:
				self._current_action_code = "000"
				self._mmf_print = False
		elif event == Events.PRINT_CANCELLED:
			if self._mmf_print and not self._settings.get_boolean(["bypass_bed_clear"]): # Send message back to UI to confirm clearing of bed.
				self._settings.set_boolean(["mmf_print_cancelled"],True)
				self._settings.save()
				self._plugin_manager.send_plugin_message(self._identifier, dict(mmf_print_cancelled=True))
			else:
				self._current_action_code = "000"
				self._mmf_print = False
		if event == Events.PRINT_PAUSED:
			self._current_action_code = "102"
		if event == Events.PRINT_RESUMED:
			self._current_action_code = "101"

	##~~ StartupPlugin mixin

	def on_startup(self, host, port):
		if self._settings.get_boolean(["mmf_print_complete"]) == False and self._settings.get_boolean(["mmf_print_cancelled"]) == False:
			self._current_action_code = "000"

		if self._settings.get_boolean(["registration_complete"]):
			self.mqtt_connect()
		else:
			self._settings.set(["supported_printers"],self.get_supported_printers())

	def on_after_startup(self):
		if self._mqtt is None:
			return

		if self._settings.get_boolean(["registration_complete"]):
			# start repeated timer publishing current status_code
			self.mmf_status_updater = RepeatedTimer(5,self.send_status)
			self.mmf_status_updater.start()
			return

	##~~ ShutdownPlugin mixin

	def on_shutdown(self):
		self.mqtt_disconnect(force=True)

	##~~ AssetPlugin mixin

	def get_assets(self):
		return dict(
			js=["js/MyMiniFactory.js"],
			css=["css/MyMiniFactory.css"]
		)

	##~~ SimpleApiPlugin mixin

	def get_api_commands(self):
		return dict(register_printer=["manufacturer","model"],forget_printer=[],mmf_print_complete=[])

	def on_api_command(self, command, data):
		if not user_permission.can():
			return flask.make_response("Insufficient rights", 403)

		if command == "register_printer":
			# Generate serial number if it doesn't already exist.
			if self._settings.get(["printer_serial_number"]) == "":
				import uuid
				MMF_UUID = str(uuid.uuid4())
				self._settings.set(["printer_serial_number"],MMF_UUID)
			# Make API call to MyMiniFactory to generate QR code and register printer.
			url = "https://www.myminifactory.com/api/v2/printer"
			mac_address = ':'.join(("%012X" % get_mac())[i:i+2] for i in range(0, 12, 2))
			payload = "{\"manufacturer\": \"%s\",\"model\": \"%s\",\"firmware_version\": \"%s\",\"serial_number\": \"%s\",\"mac_address\": \"%s\"}" % (data["manufacturer"],data["model"],"1.0.0",self._settings.get(["printer_serial_number"]),mac_address)
			headers = {'X-Api-Key' : self._settings.get(["client_key"]),'Content-Type' : "application/json"}
			self._logger.debug("Sending data: %s with header: %s" % (payload,json.dumps(headers)))
			response = requests.post(url, data=payload, headers=headers)

			if response.status_code == 200:
				serialized_response = json.loads(response.text)
				self._logger.debug(json.dumps(serialized_response))
				if serialized_response["printer_token"] != self._settings.get(["printer_token"]):
					self.mqtt_disconnect(force=True)
					self._settings.set(["printer_token"],serialized_response["printer_token"])
					self._settings.set(["printer_manufacturer"],data["manufacturer"])
					self._settings.set(["printer_model"],data["model"])
					self._settings.set_boolean(["registration_complete"], True)
					self._settings.save()
					self.mqtt_connect()
					self.on_after_startup()
				self._plugin_manager.send_plugin_message(self._identifier, dict(qr_image_url=serialized_response["qr_image_url"],printer_serial_number=self._settings.get(["printer_serial_number"])))
			else:
				self._logger.debug("API Error: %s" % response)
				self._plugin_manager.send_plugin_message(self._identifier, dict(error=response.status_code))
				
		if command == "forget_printer":
			new_supported_printers = self.get_supported_printers()
			self.mqtt_disconnect(force=True)
			self._settings.set(["printer_serial_number"],"")
			self._settings.set(["printer_token"],"")
			self._settings.set_boolean(["registration_complete"], False)
			self._settings.set(["supported_printers"],new_supported_printers)
			self._settings.save()
			#self._plugin_manager.send_plugin_message(self._identifier, dict(printer_removed=True))
			return flask.jsonify({"printer_removed":True,"supported_printers":new_supported_printers})
			
		if command == "mmf_print_complete":
			self._mmf_print = False
			self._current_action_code = "000"
			self._settings.set_boolean(["mmf_print_complete"],False)
			self._settings.set_boolean(["mmf_print_cancelled"],False)
			self._settings.save()
			return flask.jsonify(bed_cleared=True)

	##~~ PrinterCallback

	def on_printer_add_temperature(self, data):
		if self._settings.get_boolean(["registration_complete"]):
			if data.get("tool0"):
				self._current_temp_hotend = data["tool0"]["actual"]
			if data.get("bed"):
				self._current_temp_bed = data["bed"]["actual"]

	##~~ MyMiniFactory Functions

	def get_supported_printers(self):
		url = "https://www.myminifactory.com/api/v2/printers"
		headers = {'X-Api-Key': self._settings.get(["client_key"])}
		response = requests.get(url, headers=headers)
		if response.status_code == 200:
			self._logger.debug("Received printers: %s" % response.text)
			filtered_printers = list(filter(lambda d: (d['model'] and d['automatic_slicing'] == 1), json.loads(response.text)["items"]))
			return filtered_printers
		else:
			self._logger.debug("Error getting printers: %s" % response)

	def send_status(self):
		printer_disconnected = self._printer.is_closed_or_error()
		if not printer_disconnected:
			topic = "/printers"
			printer_data = self._printer.get_current_data()
			#self._logger.debug(printer_data)

			message = dict(actionCode = 300,
						   status = self._get_current_status(),
						   printer_token = self._settings.get(["printer_token"]),
						   manufacturer = self._settings.get(["printer_manufacturer"]),
						   model = self._settings.get(["printer_model"]),
						   firmware_version = self._settings.get(["printer_firmware_version"]),
						   serial_number = self._settings.get(["printer_serial_number"]),
						   current_task_id = self._current_task_id,
						   temperature = "%s" % self._current_temp_hotend,
						   bed_temperature = "%s" % self._current_temp_bed,
						   print_progress = int(printer_data["progress"]["completion"] or 0),
						   remaining_time = int(printer_data["progress"]["printTimeLeft"] or 0),
						   total_time = int(printer_data["job"]["estimatedPrintTime"] or 0),
						   date = self._get_timestamp()
						   ) 

			self._logger.debug(message)
			self.mqtt_publish(topic,message)

	def _get_current_status(self):
		return self._printer_status[self._current_action_code]

	def _get_timestamp(self):
		timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
		return timestamp

	##~~ Printer Action Functions

	def download_file(self, action):
		# Make API call to MyMiniFactory to download gcode file.
		url = "https://www.myminifactory.com/api/v2/print-file"
		payload = dict(task_id = action["task_id"],printer_token = self._settings.get(["printer_token"]))
		headers = {'X-Api-Key': self._settings.get(["client_key"])}
		self._logger.debug("Sending parameters: %s with header: %s" % (payload,headers))
		response = requests.get(url, params=payload, headers=headers, stream=True)

		if response.status_code == 200:
			# Save file to uploads folder
			gcode_file_name = self._file_manager.sanitize_name("local",action["filename"])
			gcode_download_file = "%s/%s" % (self._settings.global_get_basefolder("uploads"),gcode_file_name)
			self._logger.debug("Saving file: %s" % gcode_download_file)
			with open(gcode_download_file, 'w') as f:
				for chunk in response.iter_content(chunk_size=1024): 
					if chunk: # filter out keep-alive new chunks
						f.write(chunk)
						#f.write(response.text)
				
			# Add downloaded file to analysisqueue
			printer_profile = self._printer_profile_manager.get("_default")
			if version.get_octoprint_version() > version.get_comparable_version("1.3.9"):
				entry = QueueEntry(gcode_file_name,gcode_download_file,"gcode","local",gcode_download_file, printer_profile, None)
			else:
				entry = QueueEntry(gcode_file_name,gcode_download_file,"gcode","local",gcode_download_file, printer_profile)
			self._analysis_queue.enqueue(entry,high_priority=True) 

			# Select file downloaded and start printing if auto_start_print is enabled and not already printing
			if self._printer.is_ready():
				self._mmf_print = True
				self._printer.select_file(gcode_file_name, False, printAfterSelect=self._settings.get_boolean(["auto_start_print"]))
			else:
				self._logger.debug("Printer not ready, not selecting file to print.")
		else:
			self._logger.debug("API Error: %s" % response)
			self._plugin_manager.send_plugin_message(self._identifier, dict(error=response.status_code))

	##~~ MQTT Functions

	def mqtt_connect(self):
		broker_url = "mqtt.myminifactory.com"
		broker_username = self._settings.get(["client_name"])
		broker_password = self._settings.get(["client_key"])
		broker_insecure_port = 1883
		broker_tls_port = 8883
		broker_port = broker_tls_port
		broker_keepalive = 60
		use_tls = True
		broker_tls_insecure = False # may need to set this to true

		import paho.mqtt.client as mqtt

		broker_protocol = mqtt.MQTTv31

		if self._mqtt is None:
			self._mqtt = mqtt.Client(protocol=broker_protocol)

		if broker_username is not None:
			self._mqtt.username_pw_set(broker_username, password=broker_password)

		if use_tls and not self._mqtt_tls_set:
			self._mqtt.tls_set() # Uses the default certification authority of the system https://pypi.org/project/paho-mqtt/#tls-set
			self._mqtt_tls_set = True

		if broker_tls_insecure and not self._mqtt_tls_set:
			self._mqtt.tls_insecure_set(broker_tls_insecure)
			broker_port = broker_insecure_port # Fallbacks to the non-secure port 1883

		self._mqtt.on_connect = self._on_mqtt_connect
		self._mqtt.on_disconnect = self._on_mqtt_disconnect
		self._mqtt.on_message = self._on_mqtt_message

		self._mqtt.connect_async(broker_url, broker_port, keepalive=broker_keepalive)
		if self._mqtt.loop_start() == mqtt.MQTT_ERR_INVAL:
			self._logger.error("Could not start MQTT connection, loop_start returned MQTT_ERR_INVAL")

	def mqtt_disconnect(self, force=False):
		if self._mqtt is None:
			return

		self._mqtt.loop_stop()

		if force:
			time.sleep(1)
			self._mqtt.loop_stop(force=True)
			if self.mmf_status_updater:
				self._logger.debug("Stopping MQTT status updates.")
				self.mmf_status_updater.cancel()

		self._logger.debug("Disconnected from MyMiniFactory.")

	def mqtt_publish(self, topic, payload, retained=False, qos=0):
		if not isinstance(payload, basestring):
			payload = json.dumps(payload)

		if self._mqtt_connected:
			self._mqtt.publish(topic, payload=payload, retain=retained, qos=qos)
			#self._logger.debug("Sent message: {topic} - {payload}".format(**locals()))
			return True
		else:
			return False

	def _on_mqtt_subscription(self, topic, message, retained=None, qos=None, *args, **kwargs):
		action = json.loads(message)
		if action["action_code"] == "100":
			self._logger.debug("received prepare command")
			self._current_action_code = "100"
			self.send_status()
			time.sleep(10)
			self._logger.debug("prepartion complete status set to free")
			self._current_action_code = "000"
			self.send_status()

		if action["action_code"] == "101":
			self._logger.debug("received print command")
			# self._current_action_code = "101"
			self._current_task_id = action["task_id"]
			self.download_file(action)

		if action["action_code"] == "102":
			self._logger.debug("received pause command")
			# self._current_action_code = "102"
			if self._current_task_id:
				self._printer.pause_print()

		if action["action_code"] == "103":
			self._logger.debug("received cancel command")
			# self._current_action_code = "103"
			if self._current_task_id:
				self._printer.cancel_print()

		if action["action_code"] == "104":
			self._logger.debug("received resume command")
			# self._current_action_code = "104"
			if self._current_task_id:
				self._printer.resume_print()

		if action["action_code"] == "300":
			self._logger.debug("received status update request")
			self.on_after_startup()

	def _on_mqtt_connect(self, client, userdata, flags, rc):
		if not client == self._mqtt:
			return

		if not rc == 0:
			reasons = [
				None,
				"Connection to MyMiniFactory refused, wrong protocol version",
				"Connection to MyMiniFactory refused, incorrect client identifier",
				"Connection to MyMiniFactory refused, server unavailable",
				"Connection to MyMiniFactory refused, bad username or password",
				"Connection to MyMiniFactory refused, not authorised"
			]

			if rc < len(reasons):
				reason = reasons[rc]
			else:
				reason = None

			self._logger.error(reason if reason else "Connection to MyMiniFactory broker refused, unknown error")
			return

		self._logger.info("Connected to MyMiniFactory")

		printer_registered = self._settings.get_boolean(["registration_complete"])
		if printer_registered:
			self._mqtt.subscribe("/printers/%s" % self._settings.get(["printer_token"]))
			self._logger.debug("Subscribed to MyMiniFactory printer topic.")

		self._mqtt_connected = True

	def _on_mqtt_disconnect(self, client, userdata, rc):
		if not client == self._mqtt:
			return

		self._logger.info("Disconnected from MyMiniFactory.")

	def _on_mqtt_message(self, client, userdata, msg):
		if not client == self._mqtt:
			return

		from paho.mqtt.client import topic_matches_sub
		if topic_matches_sub("/printers/%s" % self._settings.get(["printer_token"]), msg.topic):
			args = [msg.topic, msg.payload]
			kwargs = dict(retained=msg.retain, qos=msg.qos)
			try:
				self._on_mqtt_subscription(*args, **kwargs)
			except:
				self._logger.exception("Error while calling MyMiniFactory callback")

	##~~ Softwareupdate hook

	def get_update_information(self):
		return dict(
			MyMiniFactory=dict(
				displayName="MyMiniFactory",
				displayVersion=self._plugin_version,
				type="github_release",
				user="jneilliii",
				repo="OctoPrint-MyMiniFactory",
				current=self._plugin_version,
				pip="https://github.com/jneilliii/OctoPrint-MyMiniFactory/archive/{target_version}.zip"
			)
		)


__plugin_name__ = "MyMiniFactory"

def __plugin_load__():
	global __plugin_implementation__
	__plugin_implementation__ = MyMiniFactoryPlugin()

	global __plugin_hooks__
	__plugin_hooks__ = {
		"octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information
	}

