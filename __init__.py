# Copyright (c) 2015 Ultimaker B.V.
# Cura is released under the terms of the AGPLv3 or higher.
from . import RepetierServerOutputDevicePlugin
from . import DiscoverRepetierServerAction
from UM.i18n import i18nCatalog
catalog = i18nCatalog("cura")

def getMetaData():
    return {
        "type": "extension",
        "plugin": {
            "name": "Repetier-Server connection",
            "author": "fieldOfView, kain.tm",
            "version": "0.01",
            "description": catalog.i18nc("@info:whatsthis", "Allows sending prints to Repetier-Server and monitoring the progress"),
            "api": 3
        }
    }

def register(app):
    return {
        "output_device": RepetierServerOutputDevicePlugin.RepetierServerOutputDevicePlugin(),
        "machine_action": DiscoverRepetierServerAction.DiscoverRepetierServerAction()
    }