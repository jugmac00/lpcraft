from lpcraft.plugins.tox import ToxPlugin

# XXX jugmac00 2021-12-16: The plugin mapping should be autogenerated by a
# decorator, e.g. @register_plugin(name="<name>")
PLUGINS = {"tox": ToxPlugin}