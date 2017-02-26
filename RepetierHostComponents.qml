import UM 1.2 as UM
import Cura 1.0 as Cura

import QtQuick 2.2
import QtQuick.Controls 1.1
import QtQuick.Layouts 1.1
import QtQuick.Window 2.1

Item
{
    id: base

    property bool printerConnected: Cura.MachineManager.printerOutputDevices.length != 0
    property bool repetierServerConnected: printerConnected && Cura.MachineManager.printerOutputDevices[0].hasOwnProperty("octoprintVersion")

    Button
    {
        objectName: "openRepetierServerButton"
        height: UM.Theme.getSize("save_button_save_to_button").height
        tooltip: catalog.i18nc("@info:tooltip", "Open the Repetier-Server web interface")
        text: catalog.i18nc("@action:button", "Open Repetier-Server...")
        style: UM.Theme.styles.sidebar_action_button
        onClicked: manager.openWebPage(Cura.MachineManager.printerOutputDevices[0].baseURL)
        visible: repetierServerConnected
    }

    UM.I18nCatalog{id: catalog; name:"cura"}
}