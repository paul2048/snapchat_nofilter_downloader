import requests
import os
import piexif
import zipfile

from PIL import Image
from bs4 import BeautifulSoup
from datetime import datetime
from win32_setctime import setctime


def main():
    # Get the location of the zip file from Snapchat
    mydata = [
        fname for fname in os.listdir("./")
        if "mydata_" in fname
    ][0]

    # Unzip the dowloaded zip file from Snapchat
    try:
        with zipfile.ZipFile(mydata, 'r') as zip_ref:
            zip_ref.extractall("mydata")
    except FileNotFoundError:
        print("The zip file from Snapchat can't be found.")
        return

    # Prepare the scraping of "memories_history.html"
    with open("mydata/html/memories_history.html", "r") as f:
        soup = BeautifulSoup(f.read(), "lxml")

    # Scrape each column of the table from "memories_history.html"
    c1 = soup.select("tr td:nth-child(1)")
    c2 = soup.select("tr td:nth-child(2)")
    c3 = soup.select("tr td:nth-child(3) a")

    # Get the timestamp, extension, and authentication link for each file
    timestamps = [td.text.replace(":", "-")[:-4] for td in c1]
    extensions = [".jpg" if td.text == "PHOTO" else ".mp4" for td in c2]
    auth_links = [anchor["href"][29:-3] for anchor in c3]

    for timestamp, extension, auth_link in zip(timestamps, extensions, auth_links):
        # Get the download link of the current image/video 
        dl_link = requests.post(auth_link).text

        # Create a "media" directory if it doesn't exist already
        try:
            os.mkdir("media")
        except FileExistsError:
            pass

        # Download and save the image/video in the "media" directory
        with open(f"media/{timestamp}{extension}", "wb") as f:
            try:
                # Download the image/video
                response = requests.get(dl_link)
                f.write(response.content)

                # Edit the date and time of the time when the image was taken
                if extension == ".jpg":
                    img = Image.open(f"media/{timestamp}{extension}")
                    # Create an empty exif data and assign it to the image because
                    # the files downloaded from Snapchat don't have exif data
                    img.info["exif"] = piexif.dump({})
                    # Convert the exif data from `bytes` to `dict`
                    exif_dict = piexif.load(img.info["exif"])
                    # Assign the correct date and time to the exif `dict`
                    new_timestamp = timestamp.replace("-", ":")
                    exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = new_timestamp
                    # Convert the exif `dict` back to `bytes` and save the image
                    exif_bytes = piexif.dump(exif_dict)
                    img.save(f"media/{timestamp}{extension}", "jpeg", exif=exif_bytes)
                # Edit the date and time of the time when the video was recorded
                else:
                    # The number of seconds from 1970:01:01 00:00:00 UTC to the
                    # recording date (required as the second argument for `setctime`)
                    secs = datetime.strptime(timestamp, "%Y-%m-%d %H-%M-%S").timestamp()
                    # Save the video in the media directory
                    setctime(f"media/{timestamp}{extension}", secs)
                print(f"Downloaded {timestamp}{extension}")
            # If `dl_link` is an invalid URL
            except requests.exceptions.MissingSchema:
                print(f"Can't download {timestamp}{extension}")


if __name__ == "__main__":
    main()
