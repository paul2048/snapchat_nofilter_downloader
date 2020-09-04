import requests
import os
import re

from requests_testadapter import Resp
from bs4 import BeautifulSoup


# 
with open("html/memories_history.html") as f:
    soup = BeautifulSoup(f.read(), "lxml")
    
# 
dates = [td.text for td in soup.select("tr td:first-child")]
media_types = [td.text for td in soup.select("tr td:nth-child(2)")]
auth_links = [
    # Extract the URL from the string
    anchor["href"][29:-3]
    for anchor in soup.select("tr td:nth-child(3) a")
]

for date, media_type, auth_link in zip(dates, media_types, auth_links):
    # Get the download link of the current photo/video 
    dl_link = requests.post(auth_link).text
    # The file extension of the current photo/video
    extension = ".jpg" if media_type == "PHOTO" else ".mp4"

    # Create a media folder if it doesn't exist already
    try:
        os.mkdir("media")
    except FileExistsError:
        pass

    # 
    with open(f"media/{date}{extension}", "wb") as f:
        try:
            response = requests.get(dl_link)
            f.write(response.content)
        except requests.exceptions.MissingSchema:
            pass
