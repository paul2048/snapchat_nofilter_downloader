import requests
import os
import piexif
import zipfile

from PIL import Image
from bs4 import BeautifulSoup
from datetime import datetime


def main():
    # Keep track of the number of images downloaded and not downloaded
    images_dls = 0
    error_dls = 0

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

    # Get the timestamp, media type, and authentication link for each file
    timestamps = [td.text.replace(":", "-")[:-4] for td in c1]
    media_types = [td.text for td in c2]
    auth_links = [anchor["href"][29:-3] for anchor in c3]

    for timestamp, media_type, auth_link in zip(timestamps, media_types, auth_links):
        if media_type == "PHOTO":
            # Get the download link of the current image/video 
            dl_link = requests.post(auth_link).text

            # Create a "media" directory if it doesn't exist already
            try:
                os.mkdir("media")
            except FileExistsError:
                pass

            # Download and save the image in the "media" directory
            with open(f"media/{timestamp}.jpg", "wb") as f:
                try:
                    # Download the image
                    response = requests.get(dl_link)
                    f.write(response.content)

                    img = Image.open(f"media/{timestamp}.jpg")
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
                    img.save(f"media/{timestamp}.jpg", "jpeg", exif=exif_bytes)
                    images_dls += 1
                    print(f"Downloaded {timestamp}.jpg")
                # If `dl_link` is an invalid URL
                except requests.exceptions.MissingSchema:
                    error_dls += 1
                    print(f"Can't download {timestamp}.jpg")

    print(f"Images downloaded:      {images_dls}")
    print(f"Unsuccessful downloads: {error_dls}")


if __name__ == "__main__":
    main()
