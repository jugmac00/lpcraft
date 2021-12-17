import pluggy

from lpcraft.config import Job
from lpcraft.errors import YAMLError
from lpcraft.plugin import hookspecs
from lpcraft.plugin.lib import InternalPlugins
from lpcraft.plugins import PLUGINS


def get_plugin_manager(job: Job) -> pluggy.PluginManager:
    pm = pluggy.PluginManager("lpcraft")
    pm.add_hookspecs(hookspecs)

    # register internal plugins
    pm.register(InternalPlugins(job))

    # register builtin plugins
    if job.plugin:
        if job.plugin not in PLUGINS:
            raise YAMLError("Unknown plugin")
        pm.register(PLUGINS[job.plugin](job))

    return pm