project(RepetierServerPlugin NONE)
cmake_minimum_required(VERSION 2.8.12)

install(FILES
    __init__.py
    DiscoverReptierServerAction.py
    DiscoverRepetierServerAction.qml
    RepetierServerComponents.qml
    RepetierServerOutputDevice.py
    RepetierOutputDevicePlugin.py
    LICENSE
    README.md
    DESTINATION lib/cura/plugins/RepetierServerPlugin
)
