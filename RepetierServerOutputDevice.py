from UM.i18n import i18nCatalog
from UM.Application import Application
from UM.Logger import Logger
from UM.Signal import signalemitter
from UM.Message import Message
from UM.Util import parseBool

from cura.PrinterOutputDevice import PrinterOutputDevice, ConnectionState

from PyQt5.QtNetwork import QHttpMultiPart, QHttpPart, QNetworkRequest, QNetworkAccessManager, QNetworkReply
from PyQt5.QtCore import QUrl, QTimer, pyqtSignal, pyqtProperty, pyqtSlot, QCoreApplication
from PyQt5.QtGui import QImage, QDesktopServices

import json
from time import time

i18n_catalog = i18nCatalog("cura")


##  Repetier-Server connected (wifi / lan) printer using the Repetier-Server API
@signalemitter
class RepetierServerOutputDevice(PrinterOutputDevice):
    def __init__(self, key, address, port, properties):
        super().__init__(key)

        self._address = address
        self._port = port
        self._path = properties["path"] if "path" in properties else "/"
        self._key = key
        self._properties = properties  # Properties dict as provided by zero conf

        self._gcode = None
        self._auto_print = True

        ##  Todo: Hardcoded value now; we should probably read this from the machine definition and Repetier-Server.
        self._num_extruders_set = False
        self._num_extruders = 1
        self._slug = "fanera1"
        self._api_version = "1"
        self._api_prefix = "printer/api/" + self._slug
        self._api_header = "X-Api-Key"
        self._api_key = None

        self._base_url = "http://%s:%d%s" % (self._address, self._port, self._path)
        self._api_url = self._base_url + self._api_prefix
        self._model_url = self._base_url + "printer/model/" + self._slug + "?a=upload"
        self._camera_url = "http://%s:8080/?action=snapshot" % self._address

        self.setPriority(2) # Make sure the output device gets selected above local file output
        self.setName(key)
        self.setShortDescription(i18n_catalog.i18nc("@action:button", "Print with Repetier-Server"))
        self.setDescription(i18n_catalog.i18nc("@properties:tooltip", "Print with Repetier-Server"))
        self.setIconName("print")
        self.setConnectionText(i18n_catalog.i18nc("@info:status", "Connected to Repetier-Server on {0}").format(self._key))

        #   QNetwork manager needs to be created in advance. If we don't it can happen that it doesn't correctly
        #   hook itself into the event loop, which results in events never being fired / done.
        self._manager = QNetworkAccessManager()
        self._manager.finished.connect(self._onRequestFinished)

        ##  Hack to ensure that the qt networking stuff isn't garbage collected (unless we want it to)
        self._printer_request = None
        self._printer_reply = None

        self._print_job_request = None
        self._print_job_reply = None

        self._image_request = None
        self._image_reply = None

        self._post_request = None
        self._post_reply = None
        self._post_multi_part = None
        self._post_part = None

        self._job_request = None
        self._job_reply = None

        self._command_request = None
        self._command_reply = None

        self._progress_message = None
        self._error_message = None
        self._connection_message = None

        self._update_timer = QTimer()
        self._update_timer.setInterval(2000)  # TODO; Add preference for update interval
        self._update_timer.setSingleShot(False)
        self._update_timer.timeout.connect(self._update)

        self._camera_timer = QTimer()
        self._camera_timer.setInterval(500)  # Todo: Add preference for camera update interval
        self._camera_timer.setSingleShot(False)
        self._camera_timer.timeout.connect(self._update_camera)

        self._camera_image_id = 0

        self._camera_image = QImage()

        self._connection_state_before_timeout = None

        self._last_response_time = None
        self._last_request_time = None
        self._response_timeout_time = 5
        self._recreate_network_manager_time = 30 # If we have no connection, re-create network manager every 30 sec.
        self._recreate_network_manager_count = 1

    def getProperties(self):
        return self._properties

    @pyqtSlot(str, result = str)
    def getProperty(self, key):
        key = key.encode("utf-8")
        if key in self._properties:
            return self._properties.get(key, b"").decode("utf-8")
        else:
            return ""

    ##  Get the unique key of this machine
    #   \return key String containing the key of the machine.
    @pyqtSlot(result = str)
    def getKey(self):
        return self._key

    ##  Set the API key of this Repetier-Server instance
    def setApiKey(self, api_key):
        self._api_key = api_key

    ##  Name of the instance (as returned from the zeroConf properties)
    @pyqtProperty(str, constant = True)
    def name(self):
        return self._key

    ##  Version (as returned from the zeroConf properties)
    @pyqtProperty(str, constant=True)
    def octoprintVersion(self):
        return self._properties.get(b"version", b"").decode("utf-8")

    ## IPadress of this instance
    @pyqtProperty(str, constant=True)
    def ipAddress(self):
        return self._address

    ## port of this instance
    @pyqtProperty(int, constant=True)
    def port(self):
        return self._port

    ## path of this instance
    @pyqtProperty(str, constant=True)
    def path(self):
        return self._path

    ## absolute url of this instance
    @pyqtProperty(str, constant=True)
    def baseURL(self):
        return self._base_url

    def _update_camera(self):
        ## Request new image
        url = QUrl(self._camera_url)
        self._image_request = QNetworkRequest(url)
        self._image_reply = self._manager.get(self._image_request)

    def _update(self):
        if self._last_response_time:
            time_since_last_response = time() - self._last_response_time
        else:
            time_since_last_response = 0
        if self._last_request_time:
            time_since_last_request = time() - self._last_request_time
        else:
            time_since_last_request = float("inf") # An irrelevantly large number of seconds

        # Connection is in timeout, check if we need to re-start the connection.
        # Sometimes the qNetwork manager incorrectly reports the network status on Mac & Windows.
        # Re-creating the QNetworkManager seems to fix this issue.
        if self._last_response_time and self._connection_state_before_timeout:
            if time_since_last_response > self._recreate_network_manager_time * self._recreate_network_manager_count:
                self._recreate_network_manager_count += 1
                # It can happen that we had a very long timeout (multiple times the recreate time).
                # In that case we should jump through the point that the next update won't be right away.
                while time_since_last_response - self._recreate_network_manager_time * self._recreate_network_manager_count > self._recreate_network_manager_time:
                    self._recreate_network_manager_count += 1
                Logger.log("d", "Timeout lasted over 30 seconds (%.1fs), re-checking connection.", time_since_last_response)
                self._createNetworkManager()
                return

        # Check if we have an connection in the first place.
        if not self._manager.networkAccessible():
            if not self._connection_state_before_timeout:
                Logger.log("d", "The network connection seems to be disabled. Going into timeout mode")
                self._connection_state_before_timeout = self._connection_state
                self.setConnectionState(ConnectionState.error)
                self._connection_message = Message(i18n_catalog.i18nc("@info:status",
                                                                      "The connection with the network was lost."))
                self._connection_message.show()
                # Check if we were uploading something. Abort if this is the case.
                # Some operating systems handle this themselves, others give weird issues.
                try:
                    if self._post_reply:
                        Logger.log("d", "Stopping post upload because the connection was lost.")
                        try:
                            self._post_reply.uploadProgress.disconnect(self._onUploadProgress)
                        except TypeError:
                            pass  # The disconnection can fail on mac in some cases. Ignore that.

                        self._post_reply.abort()
                        self._progress_message.hide()
                except RuntimeError:
                    self._post_reply = None  # It can happen that the wrapped c++ object is already deleted.
            return
        else:
            if not self._connection_state_before_timeout:
                self._recreate_network_manager_count = 1

        # Check that we aren't in a timeout state
        if self._last_response_time and self._last_request_time and not self._connection_state_before_timeout:
            if time_since_last_response > self._response_timeout_time and time_since_last_request <= self._response_timeout_time:
                # Go into timeout state.
                Logger.log("d", "We did not receive a response for %s seconds, so it seems Repetier-Server is no longer accesible.", time() - self._last_response_time)
                self._connection_state_before_timeout = self._connection_state
                self._connection_message = Message(i18n_catalog.i18nc("@info:status", "The connection with Repetier-Server was lost. Check your network-connections."))
                self._connection_message.show()
                self.setConnectionState(ConnectionState.error)

        ## Request 'general' printer data
        urlString = self._api_url + "?a=stateList" 
        #Logger.log("d","XXX URL: " + urlString)
        url = QUrl(urlString)
        self._printer_request = QNetworkRequest(url)
        self._printer_request.setRawHeader(self._api_header.encode(), self._api_key.encode())
        self._printer_reply = self._manager.get(self._printer_request)

        ## Request print_job data
        url = QUrl(self._api_url + "?a=listPrinter")
        self._job_request = QNetworkRequest(url)
        self._job_request.setRawHeader(self._api_header.encode(), self._api_key.encode())
        self._job_reply = self._manager.get(self._job_request)

    def _createNetworkManager(self):
        if self._manager:
            self._manager.finished.disconnect(self._onRequestFinished)

        self._manager = QNetworkAccessManager()
        self._manager.finished.connect(self._onRequestFinished)

    def close(self):
        self._updateJobState("")
        self.setConnectionState(ConnectionState.closed)
        if self._progress_message:
            self._progress_message.hide()
        if self._error_message:
            self._error_message.hide()
        self._update_timer.stop()
        self._camera_timer.stop()
        self._camera_image = QImage()
        self.newImage.emit()

    def requestWrite(self, node, file_name = None, filter_by_machine = False):
        self.writeStarted.emit(self)
        self._gcode = getattr(Application.getInstance().getController().getScene(), "gcode_list")

        self.startPrint()

    def isConnected(self):
        return self._connection_state != ConnectionState.closed and self._connection_state != ConnectionState.error

    ##  Start requesting data from the instance
    def connect(self):
        #self.close()  # Ensure that previous connection (if any) is killed.
        global_container_stack = Application.getInstance().getGlobalContainerStack()
        if not global_container_stack:
            return

        self._createNetworkManager()

        self.setConnectionState(ConnectionState.connecting)
        self._update()  # Manually trigger the first update, as we don't want to wait a few secs before it starts.

        Logger.log("d", "Connection with instance %s with ip %s started", self._key, self._address)
        self._update_timer.start()

        if parseBool(global_container_stack.getMetaDataEntry("octoprint_show_camera", False)):
            self._update_camera()
            self._camera_timer.start()
        else:
            self._camera_timer.stop()
            self._camera_image = QImage()
            self.newImage.emit()

        self._last_response_time = None
        self.setAcceptsCommands(False)
        self.setConnectionText(i18n_catalog.i18nc("@info:status", "Connecting to Repetier-Server on {0}").format(self._key))

    ##  Stop requesting data from the instance
    def disconnect(self):
        Logger.log("d", "Connection with instance %s with ip %s stopped", self._key, self._address)
        self.close()

    newImage = pyqtSignal()

    @pyqtProperty(QUrl, notify = newImage)
    def cameraImage(self):
        self._camera_image_id += 1
        # There is an image provider that is called "camera". In order to ensure that the image qml object, that
        # requires a QUrl to function, updates correctly we add an increasing number. This causes to see the QUrl
        # as new (instead of relying on cached version and thus forces an update.
        temp = "image://camera/" + str(self._camera_image_id)
        return QUrl(temp, QUrl.TolerantMode)

    def getCameraImage(self):
        return self._camera_image

    def _setJobState(self, job_state):

        # if job_state == "abort":
        #     command = "cancel"
        # elif job_state == "print":
        #     if self.jobState == "paused":
        #         command = "pause"
        #     else:
        #         command = "start"
        # elif job_state == "pause":
        #     command = "pause"

        urlString = ""
        if job_state == "abort":
            command = "cancel"
            urlString = self._api_url + '?a=stopJob'
        elif job_state == "print":
            if self.jobState == "paused":
                command = "pause"
                urlString = self._api_url + '?a=send&data={"cmd":"@pause"}'
            else:
                command = "start"
                urlString = self._api_url + '?a=continueJob'
        elif job_state == "pause":
            command = "pause"
            urlString = self._api_url + '?a=send&data={"cmd":"@pause"}'

        Logger.log("d","XXX:Command:" + command)
        Logger.log("d","XXX:Command:" + urlString)
        
        if urlString:
            url = QUrl(urlString)
            self._printer_request = QNetworkRequest(url)
            self._printer_request.setRawHeader(self._api_header.encode(), self._api_key.encode())
            self._printer_reply = self._manager.get(self._printer_request)
        # if command:
        #     self._sendCommand(command)

    def startPrint(self):
        global_container_stack = Application.getInstance().getGlobalContainerStack()
        if not global_container_stack:
            return

        self._auto_print = parseBool(global_container_stack.getMetaDataEntry("repetier_auto_print", True))
        if self._auto_print:
            Application.getInstance().showPrintMonitor.emit(True)

        if self.jobState != "ready" and self.jobState != "":
            self._error_message = Message(i18n_catalog.i18nc("@info:status", "Repetier-Server is printing. Unable to start a new job."))
            self._error_message.show()
            return
        try:
            self._progress_message = Message(i18n_catalog.i18nc("@info:status", "Sending data to Repetier-Server"), 0, False, -1)
            self._progress_message.show()

            ## Mash the data into single string
            single_string_file_data = ""
            last_process_events = time()
            for line in self._gcode:
                single_string_file_data += line
                if time() > last_process_events + 0.05:
                    # Ensure that the GUI keeps updated at least 20 times per second.
                    QCoreApplication.processEvents()
                    last_process_events = time()

            file_name = "%s.gcode" % Application.getInstance().getPrintInformation().jobName

            ##  Create multi_part request
            self._post_multi_part = QHttpMultiPart(QHttpMultiPart.FormDataType)

            ##  Create parts (to be placed inside multipart)
            # self._post_part = QHttpPart()
            # self._post_part.setHeader(QNetworkRequest.ContentDispositionHeader, "form-data; name=\"select\"")
            # self._post_part.setBody(b"true")
            # self._post_multi_part.append(self._post_part)

            # if self._auto_print:
            #     self._post_part = QHttpPart()
            #     self._post_part.setHeader(QNetworkRequest.ContentDispositionHeader, "form-data; name=\"print\"")
            #     self._post_part.setBody(b"true")
            #     self._post_multi_part.append(self._post_part)

            self._post_part = QHttpPart()
            self._post_part.setHeader(QNetworkRequest.ContentDispositionHeader, "form-data; filename=\"%s\"" % file_name)
            self._post_part.setBody(single_string_file_data.encode())
            self._post_multi_part.append(self._post_part)

            # destination = "local"
            # if parseBool(global_container_stack.getMetaDataEntry("octoprint_store_sd", False)):
            #     destination = "sdcard"
            # TODO ??

            url = QUrl(self._model_url + "&name=" + file_name)

            ##  Create the QT request
            self._post_request = QNetworkRequest(url)
            self._post_request.setRawHeader(self._api_header.encode(), self._api_key.encode())

            ##  Post request + data
            self._post_reply = self._manager.post(self._post_request, self._post_multi_part)
            self._post_reply.uploadProgress.connect(self._onUploadProgress)

            self._gcode = None

        except IOError:
            self._progress_message.hide()
            self._error_message = Message(i18n_catalog.i18nc("@info:status", "Unable to send data to Repetier-Server."))
            self._error_message.show()
        except Exception as e:
            self._progress_message.hide()
            Logger.log("e", "An exception occurred in network connection: %s" % str(e))

    def _sendCommand(self, command):
        url = QUrl(self._api_url + "job")
        self._command_request = QNetworkRequest(url)
        self._command_request.setRawHeader(self._api_header.encode(), self._api_key.encode())
        self._command_request.setHeader(QNetworkRequest.ContentTypeHeader, "application/json")

        data = "{\"command\": \"%s\"}" % command
        self._command_reply = self._manager.post(self._command_request, data.encode())
        Logger.log("d", "Sent command to Repetier-Server instance: %s", data)

    def _setTargetBedTemperature(self, temperature):
        Logger.log("d", "Setting bed temperature to %s", temperature)
        self._sendCommand("M140 S%s" % temperature)

    def _setTargetHotendTemperature(self, index, temperature):
        Logger.log("d", "Setting hotend %s temperature to %s", index, temperature)
        self._sendCommand("M104 T%s S%s" % (index, temperature))

    def _setHeadPosition(self, x, y , z, speed):
        self._sendCommand("G0 X%s Y%s Z%s F%s" % (x, y, z, speed))

    def _setHeadX(self, x, speed):
        self._sendCommand("G0 X%s F%s" % (x, speed))

    def _setHeadY(self, y, speed):
        self._sendCommand("G0 Y%s F%s" % (y, speed))

    def _setHeadZ(self, z, speed):
        self._sendCommand("G0 Y%s F%s" % (z, speed))

    def _homeHead(self):
        self._sendCommand("G28")

    def _homeBed(self):
        self._sendCommand("G28 Z")

    def _moveHead(self, x, y, z, speed):
        self._sendCommand("G91")
        self._sendCommand("G0 X%s Y%s Z%s F%s" % (x, y, z, speed))
        self._sendCommand("G90")

    ##  Handler for all requests that have finished.
    def _onRequestFinished(self, reply):
        if reply.error() == QNetworkReply.TimeoutError:
            Logger.log("w", "Received a timeout on a request to the instance")
            self._connection_state_before_timeout = self._connection_state
            self.setConnectionState(ConnectionState.error)
            return

        if self._connection_state_before_timeout and reply.error() == QNetworkReply.NoError:  #  There was a timeout, but we got a correct answer again.
            if self._last_response_time:
                Logger.log("d", "We got a response from the instance after %s of silence", time() - self._last_response_time)
            self.setConnectionState(self._connection_state_before_timeout)
            self._connection_state_before_timeout = None

        if reply.error() == QNetworkReply.NoError:
            self._last_response_time = time()

        http_status_code = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        if not http_status_code:
            # Received no or empty reply
            return

        if reply.operation() == QNetworkAccessManager.GetOperation:
            if "stateList" in reply.url().toString():  # Status update from /printer.
                if http_status_code == 200:
                    if not self.acceptsCommands:
                        self.setAcceptsCommands(True)
                        self.setConnectionText(i18n_catalog.i18nc("@info:status", "Connected to Repetier-Server on {0}").format(self._key))

                    if self._connection_state == ConnectionState.connecting:
                        self.setConnectionState(ConnectionState.connected)
                    json_data = json.loads(bytes(reply.readAll()).decode("utf-8"))

                    if not self._num_extruders_set:
                        self._num_extruders = 0
                        ## TODO
                        # while "extruder" % self._num_extruders in json_data[self._slug]:
                        #     self._num_extruders = self._num_extruders + 1

                        # Reinitialise from PrinterOutputDevice to match the new _num_extruders
                        # self._hotend_temperatures = [0] * self._num_extruders
                        # self._target_hotend_temperatures = [0] * self._num_extruders
                        # self._num_extruders_set = True
                        # TODO

                    # Check for hotend temperatures
                    # for index in range(0, self._num_extruders):
                    #     temperature = json_data[self._slug]["extruder"]["tempRead"]
                    #     self._setHotendTemperature(index, temperature)
                    temperature = json_data[self._slug]["extruder"][0]["tempRead"]
                    self._setHotendTemperature(0, temperature)

                    bed_temperature = json_data[self._slug]["heatedBed"]["tempRead"]
                    #bed_temperature_set = json_data[self._slug]["heatedBed"]["tempSet"]
                    self._setBedTemperature(bed_temperature)

                elif http_status_code == 401:
                    self.setAcceptsCommands(False)
                    self.setConnectionText(i18n_catalog.i18nc("@info:status", "Repetier-Server on {0} does not allow access to print").format(self._key))
                else:
                    pass  # TODO: Handle errors

            elif "listPrinter" in reply.url().toString():  # Status update from /job:
                if http_status_code == 200:
                    json_data = json.loads(bytes(reply.readAll()).decode("utf-8"))

                    for printer in json_data:
                        if printer["slug"]==self._slug:
                                
                            job_name = printer["job"]
                            self.setJobName(job_name)

                            job_state = "offline"
                            # if printer["state"]["flags"]["error"]:
                            #     job_state = "error"
                            if printer["paused"] == "true":
                                job_state = "paused"
                            elif job_name != "none":
                                job_state = "printing"
                                self.setProgress(printer["done"])
                            elif job_name == "none":    
                                job_state = "ready"                                
                            self._updateJobState(job_state)

                    # progress = json_data["progress"]["completion"]
                    # if progress:
                    #     self.setProgress(progress)

                    # if json_data["progress"]["printTime"]:
                    #     self.setTimeElapsed(json_data["progress"]["printTime"])
                    #     if json_data["progress"]["printTimeLeft"]:
                    #         self.setTimeTotal(json_data["progress"]["printTime"] + json_data["progress"]["printTimeLeft"])
                    #     elif json_data["job"]["estimatedPrintTime"]:
                    #         self.setTimeTotal(max(json_data["job"]["estimatedPrintTime"], json_data["progress"]["printTime"]))
                    #     elif progress > 0:
                    #         self.setTimeTotal(json_data["progress"]["printTime"] / (progress / 100))
                    #     else:
                    #         self.setTimeTotal(0)
                    # else:
                    #     self.setTimeElapsed(0)
                    #     self.setTimeTotal(0)
                    # self.setJobName(json_data["job"]["file"]["name"])
                else:
                    pass  # TODO: Handle errors

            elif "snapshot" in reply.url().toString():  # Update from camera:
                if http_status_code == 200:
                    self._camera_image.loadFromData(reply.readAll())
                    self.newImage.emit()
                else:
                    pass  # TODO: Handle errors

        elif reply.operation() == QNetworkAccessManager.PostOperation:
            if "model" in reply.url().toString():  # Result from /files command:
                if http_status_code == 201:
                    Logger.log("d", "Resource created on Repetier-Server instance: %s", reply.header(QNetworkRequest.LocationHeader).toString())
                else:
                    pass  # TODO: Handle errors

                json_data = json.loads(bytes(reply.readAll()).decode("utf-8"))

                modelList = json_data["data"];

                
                lastModel = modelList[len(modelList)-1]
                # Logger.log("d", "XXX1:len"+str(len(modelList)))
                # Logger.log("d", "XXX1:lastModel"+str(lastModel))
                modelId = lastModel["id"]

                # "http://%s:%d%s" % (self._address, self._port, self._path)
                urlString = self._api_url + '?a=copyModel&data={"id": %s}' % (modelId) 
                Logger.log("d", "XXX1: modelId: "+str(modelId))
                url = QUrl(urlString)
                self._printer_request = QNetworkRequest(url)
                self._printer_request.setRawHeader(self._api_header.encode(), self._api_key.encode())
                self._printer_reply = self._manager.get(self._printer_request)
                Logger.log("d", "XXX1: modelId: "+str(urlString))

                reply.uploadProgress.disconnect(self._onUploadProgress)
                self._progress_message.hide()
                global_container_stack = Application.getInstance().getGlobalContainerStack()
                if not self._auto_print:
                    location = reply.header(QNetworkRequest.LocationHeader)
                    if location:
                        file_name = QUrl(reply.header(QNetworkRequest.LocationHeader).toString()).fileName()
                        message = Message(i18n_catalog.i18nc("@info:status", "Saved to Repetier-Server as {0}").format(file_name))
                    else:
                        message = Message(i18n_catalog.i18nc("@info:status", "Saved to Repetier-Server"))
                    message.addAction("open_browser", i18n_catalog.i18nc("@action:button", "Open Repetier-Server..."), "globe",
                                        i18n_catalog.i18nc("@info:tooltip", "Open the Repetier-Server web interface"))
                    message.actionTriggered.connect(self._onMessageActionTriggered)
                    message.show()

            elif "job" in reply.url().toString():  # Result from /job command:
                if http_status_code == 204:
                    Logger.log("d", "Repetier-Server command accepted")
                else:
                    pass  # TODO: Handle errors

        else:
            Logger.log("d", "RepetierServerOutputDevice got an unhandled operation %s", reply.operation())

    def _onUploadProgress(self, bytes_sent, bytes_total):
        if bytes_total > 0:
            # Treat upload progress as response. Uploading can take more than 10 seconds, so if we don't, we can get
            # timeout responses if this happens.
            self._last_response_time = time()

            progress = bytes_sent / bytes_total * 100
            if progress < 100:
                if progress > self._progress_message.getProgress():
                    self._progress_message.setProgress(progress)
            else:
                self._progress_message.hide()
                self._progress_message = Message(i18n_catalog.i18nc("@info:status", "Storing data on Repetier-Server"), 0, False, -1)
                self._progress_message.show()
        else:
            self._progress_message.setProgress(0)

    def _onMessageActionTriggered(self, message, action):
        if action == "open_browser":
            QDesktopServices.openUrl(QUrl(self._base_url))