"""
<plugin key="Tibber" name="Tibber 1.02" author="Processware" version="1.02" wikilink="https://github.com/me-processware/Tibber-Domoticz" externallink="">
    <description>
        <h2>Tibber API is used to fetch data from Tibber.com</h2><br/>
        <h3>Changelog</h3>
        <ul style="list-style-type:square">
            <li>v1.02 - Added configurable timezone support and improved price calculations</li>
            <li>v1.01 - Initial release</li>
        </ul>
        <h3>Configuration</h3>
        <h3>Support Development</h3>
        <div style="display: flex; align-items: center;">
            <p style="margin-right: 10px;">
                If you find this plugin helpful, consider supporting its development.<br/>
                A lot of time and effort has been put into this plugin.<br/>
                I'm not a programmer and had to figure out a lot :)<br/>
                Luckily, my ChatGPT Code Buddy helped me a lot and made it all possible.<br/>
                
                <a href="https://www.paypal.com/donate/?hosted_button_id=SDKNVS3CQ9R4N">Click here to Donate via PayPal</a>. Or scan the QR-code to donate:
            </p>
            <img src="https://processware.datadrop.nl/index.php/s/TiATwJWYNMrb7EG/download/QR-code.png" width="150" height="150" alt="Donate via PayPal QR Code"/>
        </div>
        <h3>Features</h3>
        <ul style="list-style-type:square">
            <li>Fetch current price including taxes, minimum power, maximum power, average power, accumulated cost, and accumulated consumption, updated hourly at the start of every hour</li>
            <li>Fetch today's minimum, maximum, and mean price including taxes</li>
            <li>Fetch current Power data live if you have Tibber Watty/Pulse installed</li>
        </ul>
        <h3>Devices</h3>
        <ul style="list-style-type:square">
            <li>Creates a Custom Sensor with name "xxxxx - Price"</li>
        </ul>
        <h3>How to get your personal Tibber Access Token?</h3>
        <ul style="list-style-type:square">
            <li>Login to create your personal token: &<a href="https://developer.tibber.com">https://developer.tibber.com</a></li>
            <li>Copy your Tibber Access Token to the field below</li>
        </ul>
        <h4>Default Tibber Access Token and Home ID are demo copied from &<a href="https://developer.tibber.com/explorer">https://developer.tibber.com/explorer</a></h4><br/>
    </description>
    <params>
        <param field="Mode1" label="Tibber Access Token" width="460px" required="true" default="5K4MVS-OjfWhK_4yrjOlFe1F6kJXPVf7eQYggo8ebAE"/>
        <param field="Mode4" label="Home ID" width="350px" required="false" default="96a14971-525a-4420-aae9-e5aedaa129ff"/>
        <param field="Mode5" label="Create device for Pulse/Watty" width="50px">
            <options>
                <option label="Yes" value="Yes" default="true" />
                <option label="No" value="No"/>
            </options>
        </param>
        <param field="Mode6" label="Enable Logging" width="75px">
            <options>
                <option label="Yes" value="Yes" default="true"/>
                <option label="No" value="No"/>
            </options>
        </param>
        <param field="Mode7" label="Timezone" width="200px">
            <options>
                <option label="Europe/Amsterdam" value="Europe/Amsterdam" default="true"/>
                <option label="Europe/London" value="Europe/London"/>
                <option label="Europe/Berlin" value="Europe/Berlin"/>
                <option label="Europe/Oslo" value="Europe/Oslo"/>
                <option label="Europe/Stockholm" value="Europe/Stockholm"/>
            </options>
        </param>
    </params>
</plugin>

"""
import Domoticz
import traceback
import sys
import os
import json
import requests
import threading
import asyncio
import time
import random
from datetime import datetime
import pytz
from gql import Client, gql
from gql.transport.websockets import WebsocketsTransport

class BasePlugin:
    enabled = False

    def __init__(self):
        self.retry_delay = 1  # Initial delay for exponential backoff
        self.max_retries = 5  # Maximum retries
        self.retry_count = 0
        self.max_retry_delay = 60  # Maximum retry delay
        self.web_socket_thread = None  # Thread to handle WebSocket
        self.stop_thread = threading.Event()  # Event to signal the WebSocket thread to stop
        self.loop = None  # Event loop for the WebSocket thread
        self.last_fetch_hour = None  # Track the last hour of data fetch
        self.connection_count = 0  # Counter for WebSocket connections
        self.reconnect_count = 0  # Counter for WebSocket reconnects

    def onStop(self):
        Domoticz.Log("Stopping plugin...")

        self.stop_thread.set()  # Signal the WebSocket thread to stop
        if self.web_socket_thread and self.web_socket_thread.is_alive():
            self.web_socket_thread.join(timeout=10)  # Ensure the WebSocket thread stops

        # Disconnect all active connections
        if self.GetDataCurrent.Connected() or self.GetDataCurrent.Connecting():
            self.GetDataCurrent.Disconnect()
        if self.GetDataMiniMaxMean.Connected() or self.GetDataMiniMaxMean.Connecting():
            self.GetDataMiniMaxMean.Disconnect()
        if self.CheckRealTimeHardware.Connected() or self.CheckRealTimeHardware.Connecting():
            self.CheckRealTimeHardware.Disconnect()
        if self.GetHomeID.Connected() or self.GetHomeID.Connecting():
            self.GetHomeID.Disconnect()
        if self.GetHouseNumber.Connected() or self.GetHouseNumber.Connecting():
            self.GetHouseNumber.Disconnect()

        # Handle stopping of the event loop safely
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.loop.stop)  # Stop the event loop safely
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
                try:
                    self.loop.run_until_complete(task)
                except asyncio.CancelledError:
                    pass
            self.loop.close()
            self.loop = None  # Reset the loop reference

        Domoticz.Log("Plugin stopped.")

    def onStart(self):
        Domoticz.Log("onStart")
        self.AccessToken = Parameters["Mode1"]
        self.HomeID = Parameters["Mode4"]
        self.CreateRealTime = Parameters["Mode5"]
        self.EnableLogging = Parameters["Mode6"]
        # Set default timezone if Mode7 is not available
        self.Timezone = Parameters.get("Mode7", "Europe/Amsterdam")

        self.headers = {
            'Host': 'api.tibber.com',
            'Authorization': 'Bearer ' + self.AccessToken,  # Tibber Token
            'Content-Type': 'application/json',
            'User-Agent': 'Domoticz/2024.08.22 TibberPlugin/1.02'  # Updated User-Agent
        }

        # Validate the Tibber Access Token by making a simple API call before proceeding
        if not self.validate_token():
            Domoticz.Error("Invalid Tibber Access Token. Please check your credentials.")
            return

        self.GetHomeID = Domoticz.Connection(Name="Get HomeID", Transport="TCP/IP", Protocol="HTTPS", Address="api.tibber.com", Port="443")
        if not _plugin.GetHomeID.Connected() and not _plugin.GetHomeID.Connecting() and not self.HomeID:
            _plugin.GetHomeID.Connect()

        self.GetHouseNumber = Domoticz.Connection(Name="Get House Number", Transport="TCP/IP", Protocol="HTTPS", Address="api.tibber.com", Port="443")
        if not _plugin.GetHouseNumber.Connected() and not _plugin.GetHouseNumber.Connecting() and self.HomeID:
            _plugin.GetHouseNumber.Connect()

        self.CheckRealTimeHardware = Domoticz.Connection(Name="Check Real Time Hardware", Transport="TCP/IP", Protocol="HTTPS", Address="api.tibber.com", Port="443")
        self.GetDataCurrent = Domoticz.Connection(Name="Get Current", Transport="TCP/IP", Protocol="HTTPS", Address="api.tibber.com", Port="443")
        self.GetSubscription = Domoticz.Connection(Name="Get Subscription", Transport="TCP/IP", Protocol="HTTPS", Address="api.tibber.com", Port="443")
        self.GetDataMiniMaxMean = Domoticz.Connection(Name="Get MiniMaxMean", Transport="TCP/IP", Protocol="HTTPS", Address="api.tibber.com", Port="443")

        # Use the static WebSocket URL for Tibber
        self.web_socket_url = "wss://websocket-api.tibber.com/v1-beta/gql/subscriptions"

        # Start WebSocket operations in a separate thread if real-time consumption is enabled
        if self.CreateRealTime == "Yes":
            self.web_socket_thread = threading.Thread(target=self.run_websocket)
            self.web_socket_thread.start()

        # Create devices on startup and fetch initial price information
        self.create_devices()
        self.fetch_price_info()  # Fetch the initial price data

    def onConnect(self, Connection, Status, Description):
        if Status == 0:  # Successful connection
            Domoticz.Log(f"Successfully connected to {Connection.Name}")
        else:
            Domoticz.Error(f"Failed to connect to {Connection.Name}: {Description}")

    def validate_token(self):
        """
        Make a simple API request to validate the Tibber Access Token before proceeding.
        """
        try:
            response = requests.post(
                'https://api.tibber.com/v1-beta/gql',
                headers=self.headers,
                json={"query": "{viewer {name}}"}
            )  # A simple query to validate the token

            if response.status_code == 200:
                Domoticz.Log("Tibber Access Token is valid.")
                return True
            else:
                Domoticz.Error(f"Failed to validate Tibber Access Token: {response.status_code}, {response.text}")
                return False
        except Exception as e:
            Domoticz.Error(f"Error validating Tibber Access Token: {str(e)}")
            return False

    def create_devices(self):
        """
        Create the required devices on startup if they do not exist, 
        but only if the 'CreateRealTime' mode is set to 'Yes'.
        """
        if self.CreateRealTime != "Yes":
            Domoticz.Log("Device creation skipped as per configuration (Mode5: No).")
            return

        # Create live measurement devices with proper units
        live_measurement_devices = [
            "power", "minPower", "maxPower", "averagePower", "accumulatedCost", 
            "accumulatedConsumption", "accumulatedProduction", "powerProduction",
            "accumulatedConsumptionLastHour", "accumulatedProductionLastHour",
            "accumulatedReward", "lastMeterConsumption", "powerReactive",
            "powerProductionReactive", "minPowerProduction", "maxPowerProduction",
            "voltagePhase1", "voltagePhase2", "voltagePhase3", "currentL1",
            "currentL2", "currentL3", "lastMeterProduction"
        ]
        
        for name in live_measurement_devices:
            if name not in Devices:
                UpdateDevice(name, "0")

    def onHeartbeat(self):
        """
        Called periodically by Domoticz to perform tasks on a regular interval.
        """
        current_time = datetime.now()
        if self.last_fetch_hour is None or current_time.hour != self.last_fetch_hour:
            self.last_fetch_hour = current_time.hour
            Domoticz.Log("Fetching price information on the hour...")
            self.fetch_price_info()

    def fetch_price_info(self):
        """
        Fetches price information using the Tibber API and updates corresponding devices.
        Uses Europe/Amsterdam timezone for correct price calculations.
        """
        # Set timezone based on configuration
        local_tz = pytz.timezone(self.Timezone)
        now = datetime.now(local_tz)
        
        query = """
        query {
          viewer {
            homes {
              currentSubscription {
                priceInfo {
                  current {
                    total
                    energy
                    tax
                    startsAt
                  }
                  today {
                    total
                    energy
                    tax
                    startsAt
                  }
                  tomorrow {
                    total
                    energy
                    tax
                    startsAt
                  }
                }
              }
            }
          }
        }
        """

        try:
            response = requests.post(
                'https://api.tibber.com/v1-beta/gql',
                headers=self.headers,
                json={'query': query}
            )

            if response.status_code == 200:
                price_info = response.json()['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']

                # Log the price information for debugging, if logging is enabled
                if self.EnableLogging == "Yes":
                    Domoticz.Log(f"Fetched Price Information: {json.dumps(price_info, indent=2)}")

                # Calculate daily average price (mean)
                today_prices = price_info['today']
                total_price = sum(price['total'] for price in today_prices)
                mean_price = total_price / len(today_prices) if today_prices else 0

                # Find minimum and maximum prices
                min_price = min((price['total'] for price in today_prices), default=0)
                max_price = max((price['total'] for price in today_prices), default=0)

                # Update devices with the fetched price information
                UpdateDevice('Current Price', round(price_info['current']['total'], 3))
                UpdateDevice('Mean Price', round(mean_price, 3))
                UpdateDevice('Current Price excl. fee', round(price_info['current']['energy'], 3))
                UpdateDevice('Minimum Price', round(min_price, 3))
                UpdateDevice('Maximum Price', round(max_price, 3))

                # Check if data for tomorrow is available
                if price_info['tomorrow']:
                    UpdateDevice('Tomorrow Price', round(price_info['tomorrow'][0]['total'], 3))

            else:
                Domoticz.Error(f"Error fetching price information: {response.status_code}, {response.text}")

        except Exception as e:
            Domoticz.Error(f"Error fetching price information: {str(e)}")

    def run_websocket(self):
        """
        Runs the WebSocket subscription in a separate thread with its own event loop.
        """
        if not self.web_socket_url:
            Domoticz.Error("WebSocket URL not available, cannot start subscription.")
            return

        while not self.stop_thread.is_set():
            try:
                # Create a new asyncio event loop
                self.loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self.loop)

                # Run the websocket subscription within the event loop
                self.loop.run_until_complete(self.websocket_subscription())

            except Exception as e:
                Domoticz.Error(f"WebSocket error: {str(e)}")
                self.handle_reconnect()

    async def websocket_subscription(self):
        """
        Asynchronous method to handle the WebSocket subscription.
        """
        try:
            async with Client(
                transport=WebsocketsTransport(
                    url=self.web_socket_url,
                    init_payload={"token": self.AccessToken},
                    headers=self.headers,
                    subprotocols=["graphql-transport-ws"],
                    ping_interval=10
                )
            ) as session:
                self.connection_count += 1  # Increment connection count
                Domoticz.Log(f"WebSocket connection established. Total connections: {self.connection_count}")

                query = gql(
                    f"""
                    subscription {{
                        liveMeasurement(homeId: "{self.HomeID}") {{
                            power
                            lastMeterConsumption
                            accumulatedConsumption
                            accumulatedProduction
                            accumulatedConsumptionLastHour
                            accumulatedProductionLastHour
                            accumulatedCost
                            accumulatedReward
                            minPower
                            averagePower
                            maxPower
                            powerProduction
                            powerReactive
                            powerProductionReactive
                            minPowerProduction
                            maxPowerProduction
                            lastMeterProduction
                            voltagePhase1
                            voltagePhase2
                            voltagePhase3
                            currentL1
                            currentL2
                            currentL3
                        }}
                    }}
                    """
                )
                async for result in session.subscribe(query):
                    for name, value in result["liveMeasurement"].items():
                        if value is not None:
                            # Ensure the value is a proper float or int
                            UpdateDevice(str(name), str(round(float(value), 3)))
                    self.retry_count = 0  # Reset retry count on successful message receipt
                    self.retry_delay = 1  # Reset delay on success
                
        except Exception as e:
            Domoticz.Error(f"WebSocket error during async operation: {str(e)}")
            self.loop.stop()  # Stop the event loop on error

    def handle_reconnect(self):
        """
        Handles reconnecting with exponential backoff and jitter.
        """
        self.retry_count += 1
        self.retry_delay = min(self.max_retry_delay, self.retry_delay * 2)
        jitter = random.uniform(0, self.retry_delay)
        self.reconnect_count += 1  # Increment reconnect count
        Domoticz.Log(f"Reconnecting in {jitter:.2f} seconds (Attempt {self.retry_count}/{self.max_retries}). Total reconnects: {self.reconnect_count}")
        time.sleep(jitter)

        if self.retry_count >= self.max_retries:
            Domoticz.Error("Max retries reached, stopping reconnect attempts")
            return

        # Check if real-time consumption is still enabled
        self.real_time_enabled = True  # Assuming real-time consumption is still enabled

    def disconnect_all(self):
        """ Cleanly disconnects all active connections. """
        if _plugin.GetDataCurrent.Connected() or _plugin.GetDataCurrent.Connecting():
            _plugin.GetDataCurrent.Disconnect()
        if _plugin.GetDataMiniMaxMean.Connected() or _plugin.GetDataMiniMaxMean.Connecting():
            _plugin.GetDataMiniMaxMean.Disconnect()
        if _plugin.CheckRealTimeHardware.Connected() or _plugin.CheckRealTimeHardware.Connecting():
            _plugin.CheckRealTimeHardware.Disconnect()
        if _plugin.GetHomeID.Connected() or _plugin.GetHomeID.Connecting():
            _plugin.GetHomeID.Disconnect()
        if _plugin.GetHouseNumber.Connected() or _plugin.GetHouseNumber.Connecting():
            _plugin.GetHouseNumber.Disconnect()

def UpdateDevice(Name, sValue):
    device_map = {
        "Current Price": (1, "Custom"),
        "Mean Price": (2, "Custom"),
        "Current Price excl. fee": (3, "Custom"),
        "Minimum Price": (4, "Custom"),
        "Maximum Price": (5, "Custom"),
        "Tomorrow Price": (6, "Custom"),
        "power": (7, "Watt"),
        "minPower": (8, "Watt"),
        "maxPower": (9, "Watt"),
        "averagePower": (10, "Watt"),
        "accumulatedCost": (11, "Custom"),
        "accumulatedConsumption": (12, "kWh"),
        "accumulatedProduction": (13, "kWh"),
        "powerProduction": (14, "Watt"),
        "accumulatedConsumptionLastHour": (15, "kWh"),
        "accumulatedProductionLastHour": (16, "kWh"),
        "accumulatedReward": (17, "Custom"),
        "lastMeterConsumption": (18, "kWh"),
        "powerReactive": (19, "VAR"),
        "powerProductionReactive": (20, "VAR"),
        "minPowerProduction": (21, "Watt"),
        "maxPowerProduction": (22, "Watt"),
        "voltagePhase1": (23, "Voltage"),
        "voltagePhase2": (24, "Voltage"),
        "voltagePhase3": (25, "Voltage"),
        "currentL1": (26, "Current"),
        "currentL2": (27, "Current"),
        "currentL3": (28, "Current"),
        "lastMeterProduction": (29, "kWh")
    }

    if Name not in device_map:
        Domoticz.Error(f"Unknown device name: {Name}")
        return

    ID, UnitType = device_map[Name]

    # Device Type and SubType mappings based on unit
    type_map = {
        "Watt": (248, 1),        # Electric (Power in Watts)
        "kWh": (243, 29),        # Electric (Energy in kWh)
        "Current": (243, 23),    # Current (Amps)
        "Voltage": (243, 8),     # Voltage (Volts)
        "VAR": (243, 33),        # Reactive Power (VAR)
        "Custom": (243, 31),     # Custom Sensor
    }

    Type, SubType = type_map.get(UnitType, (243, 31))  # Default to Custom if not mapped

    # Check if device needs to be created
    if ID not in Devices:
        if UnitType == "kWh":
            Domoticz.Device(Name=Name, Unit=ID, Type=Type, Subtype=SubType, Switchtype=4, Options={'EnergyMeterMode': 'From Device'}, Used=1).Create()
        else:
            Domoticz.Device(Name=Name, Unit=ID, Type=Type, Subtype=SubType, Used=1).Create()

    # Update device with sValue
    if UnitType == "kWh":
        # Ensure sValue is in the correct format: "Power in Watt;Energy in kWh"
        sValue = f"0;{sValue}"  # Assuming only energy is updated
        Devices[ID].Update(nValue=0, sValue=sValue)
    else:
        if ID in Devices and Devices[ID].sValue != sValue:
            Devices[ID].Update(nValue=0, sValue=str(sValue))

global _plugin
_plugin = BasePlugin()

def onStart():
    global _plugin
    _plugin.onStart()

def onStop():
    global _plugin
    _plugin.onStop()

def onConnect(Connection, Status, Description):
    global _plugin
    _plugin.onConnect(Connection, Status, Description)

def onMessage(Connection, Data):
    _plugin.onMessage(Connection, Data)

def onHeartbeat():
    global _plugin
    _plugin.onHeartbeat()
