"""Constants for the EBO for Home Assistant integration."""

DOMAIN = "ebo"

# Per-robot config-entry data (built from the add-on's data API).
CONF_NODE = "node"
CONF_NAME = "name"
CONF_SN = "sn"
CONF_MAC = "mac"
CONF_MODEL = "model"
CONF_RTSP = "rtsp"
CONF_API = "api"      # add-on data/command API base URL
CONF_TOKEN = "token"  # token for that API

# The add-on's slug ends with this (Supervisor prefixes a per-repository hash); its options carry
# the api_token, and its info carries the internal hostname we build the API URL from.
ADDON_SLUG_SUFFIX = "_ebo"
CONF_API_TOKEN = "api_token"
API_PORT = 8098
