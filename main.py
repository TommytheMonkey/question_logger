import os
import re
import ssl
import certifi
import urllib.request
import base64
import requests
import json
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build, build as gdoc_build
from googleapiclient.http import MediaIoBaseDownload
from google.oauth2.service_account import Credentials
from slack_sdk.oauth import AuthorizeUrlGenerator


SLACK_CHANNEL_IDS = {
    "ewing_internal": "C08HUKW7NDU",
    "ewing_west": "C08N88F97BQ",
    "ewing_mountain": "C08QMQFNSG4",
    "tom-takeoff-central": "C08LQRNNRC2",
    "tom-design-central": "C08L7SS19B9",
    "tom-takeoff-east": "C09085WGE9G",
    "tom-ewing": "C08JEHW4DNC",
    "ewing-associates": "C08L3V266F3"
}