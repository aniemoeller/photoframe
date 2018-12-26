# This file is part of photoframe (https://github.com/mrworf/photoframe).
#
# photoframe is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# photoframe is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with photoframe.  If not, see <http://www.gnu.org/licenses/>.
#
import threading
import logging
import os
import random
import datetime
import hashlib
import time
import json
import math
import re
import subprocess

from modules.remember import remember
from modules.helper import helper

class slideshow:
  def __init__(self, display, settings, oauth, colormatch):
    self.queryPowerFunc = None
    self.thread = None
    self.display = display
    self.settings = settings
    self.oauth = oauth
    self.colormatch = colormatch
    self.imageCurrent = None
    self.imageMime = None
    self.void = open(os.devnull, 'wb')

  def getCurrentImage(self):
    return self.imageCurrent, self.imageMime

  def getColorInformation(self):
    return {
      'temperature':self.colormatch.getTemperature(),
      'lux':self.colormatch.getLux()
      }

  def setQueryPower(self, func):
    self.queryPowerFunc = func

  def start(self, blank=False):
    if blank:
      self.display.clear()

    if self.settings.get('oauth_token') is None:
      self.display.message('Photoalbum isn\'t ready yet\n\nPlease direct your webbrowser to\n\nhttp://%s:7777/' % self.settings.get('local-ip'))
      logging.info('You need to link your photoalbum first')
    elif self.thread is None:
      self.thread = threading.Thread(target=self.presentation)
      self.thread.daemon = True
      self.thread.start()

  def presentation(self):
    logging.info('Starting presentation')
    seen = []
    delay = 0
    while True:
      # Avoid showing images if the display is off
      if self.queryPowerFunc is not None and self.queryPowerFunc() is False:
        logging.info("Display is off, exit quietly")
        break

      imgs = cache = memory = None
      index = self.settings.getKeyword()
      tries = 20
      time_process = time.time()
      while tries > 0:
        tries -= 1
        if len(seen) == self.settings.countKeywords():
          # We've viewed all images, reset
          logging.info('All images we have keywords for have been seen, restart')
          logging.info('Seen holds: %s', repr(seen))
          logging.info('Settings.countKeywords() = %d', self.settings.countKeywords())

          for saw in seen:
            r = remember(saw, 0)
            r.debug()
            r.forget()
          r = remember('/tmp/overallmemory.json', 0)
          r.debug()
          r.forget()
          if self.settings.getUser('refresh-content') == 0:
            logging.info('Make sure we refresh all images now')
            for saw in seen:
              os.remove(saw)
          seen = []


        keyword = self.settings.getKeyword(index)
        imgs, cache = self.getImages(keyword)
        if imgs is None:
          # Try again!
          continue

        # If we've seen all images for this keyword, skip to next
        if cache in seen:
          index += 1
          if index == self.settings.countKeywords():
            index = 0
          continue

        memory = remember(cache, len(imgs))

        if not imgs or memory.seenAll():
          if not imgs:
            logging.error('Failed to load image list for keyword %s' % keyword)
          elif memory.seenAll():
            seen.append(cache)
            logging.debug('All images for keyword %s has been shown' % keyword)
          continue

        # Now, lets make sure we didn't see this before
        photo_id, mime, title, ts = self.pickImage(imgs, memory)
        if photo_id == '':
          logging.warning('No image was returned from pickImage')
          continue # Do another one (well, it means we exhausted available images for this keyword)

        # Avoid having duplicated because of overlap from keywords
        memory = remember('/tmp/overallmemory.json', 0)
        if memory.seen(photo_id):
          continue
        else:
          memory.saw(photo_id)

        ext = helper.getExtension(mime)
        if ext is not None:
          filename = os.path.join(self.settings.get('tempfolder'), 'image.%s' % ext)
          if self.downloadImage(photo_id, filename):
            self.imageCurrent = filename
            self.imageMime = mime
            break
          else:
            logging.warning('Failed to download image, trying another one')
        else:
          logging.warning('Mime type %s isn\'t supported' % mime)

      time_process = time.time() - time_process
      logging.debug('Processing time was %d seconds.' % time_process)
      # Delay before we show the image (but take processing into account)
      # This should keep us fairly consistent
      if time_process < delay:
        time.sleep(delay - time_process)
      if tries == 0:
        self.display.message('Issues showing images\n\nCheck network and settings')
      else:
        self.display.image(self.imageCurrent)
        os.remove(self.imageCurrent)

      delay = self.settings.getUser('interval')
    self.thread = None

  def pickImage(self, images, memory):
    ext = ['jpg','png','dng','jpeg','gif','bmp']
    count = len(images)

    i = random.SystemRandom().randint(0,count-1)
    while not memory.seenAll():
      proposed = images[i]['id']
      if not memory.seen(proposed):
        memory.saw(proposed)
        entry = images[i]
        # Make sure we don't get a video, unsupported for now (gif is usually bad too)
        if 'video' not in entry['mimeType']:
          break
        else:
          logging.warning('Unsupported media: %s' % entry['mimeType'])
      else:
        i += 1
        if i == count:
          i = 0

    if memory.seenAll():
      logging.error('Failed to find any image, abort')
      return ('', '', '', 0)

    
    title = ""
    photo_id = entry['id']
    timestamp = entry['mediaMetadata']['creationTime']
    mime = entry['mimeType']

    return (photo_id, mime, title, timestamp)

  def getImages(self, keyword):
    # Create filename from keyword
    filename = hashlib.new('sha1')
    filename.update(repr(keyword))
    filename = filename.hexdigest() + ".json"
    filename = os.path.join(self.settings.get('tempfolder'), filename)

    if os.path.exists(filename) and self.settings.getUser('refresh-content') > 0: # Check age!
      age = math.floor( (time.time() - os.path.getctime(filename)) / 3600)
      if age >= self.settings.getUser('refresh-content'):
        logging.debug('File too old, %dh > %dh, refreshing' % (age, self.settings.getUser('refresh-content')))
        os.remove(filename)
        # Make sure we don't remember since we're refreshing
        memory = remember(filename, 0)
        memory.forget()

    if not os.path.exists(filename):
      # check if keyword is album
      '''url = 'https://photoslibrary.googleapis.com/v1/albums'
      data = self.oauth.request(url).json()
      albumid = None
      picturecount = self.settings.getUser('picturecount')
      for i in range(len(data['albums'])):
        if 'title' in data['albums'][i] and data['albums'][i]['title'] == keyword:
          albumid = data['albums'][i]['id']
      
      if albumid is None:
        url = 'https://photoslibrary.googleapis.com/v1/sharedAlbums'
        data = self.oauth.request(url).json()
        for i in range(len(data['sharedAlbums'])):
          if 'title' in data['sharedAlbums'][i] and data['sharedAlbums'][i]['title'] == keyword:
            albumid = data['sharedAlbums'][i]['id']

      # fallback to all pictures if album not available
      if albumid is not None:
        logging.debug('Got album: %s' % keyword)
        params = {
          'albumId' : albumid,
          'pageSize' : picturecount,
        }
      else:
        logging.debug('Couldn\'t get album: %s falling back to all images.' % keyword)
        params = {
          'pageSize' : picturecount,
          'filters': {
            'mediaTypeFilter': {
              'mediaTypes': [
                'PHOTO'
              ]
            }
          }
        }
      # Request albums      
      url = 'https://photoslibrary.googleapis.com/v1/mediaItems:search'
      #logging.debug('Downloading image list for %s...' % keyword)
      data = self.oauth.request(url, params=params,post=True)
      '''
      albumid = None
      albumid = self.checkForOwnAlbum(keyword)
      if albumid is None:
        albumid = self.checkForSharedAlbum(keyword)
      data = self.getPhotoList(albumid)

      if len(data) == 0:
        logging.warning('Requesting photo failed with status code %d (%s)', data.status_code, data.reason)
        return None, filename
      with open(filename, 'w') as f:
        json.dump(data,f)
    images = None
    try:
      with open(filename) as f:
        images = json.load(f)
      logging.debug('Loaded %d images into list' % len(images))
      return images, filename
    except:
      logging.exception('Failed to load images')
      os.remove(filename)
      return None, filename

  def downloadImage(self, photo_id, dest):
    #logging.debug('Downloading %s...' % uri)
    filename, ext = os.path.splitext(dest)
    temp = "%s-org%s" % (filename, ext)
    #picture      
    url = 'https://photoslibrary.googleapis.com/v1/mediaItems/'+photo_id
    data = self.oauth.request(url)
    if data.status_code == 200:
      if self.oauth.request(data.json()['baseUrl']+"=w"+str(self.settings.getUser("width"))+"-h"+str(self.settings.getUser("height")), destination=temp):
        if self.settings.getUser('blur') == 'activated':
          helper.makeFullframe(temp, self.settings.getUser('width'), self.settings.getUser('height'))
        if self.colormatch.hasSensor():
          if not self.colormatch.adjust(temp, dest):
            logging.warning('Unable to adjust image to colormatch, using original')
            os.rename(temp, dest)
          else:
            os.remove(temp)
        else:
          os.rename(temp, dest)
        return True
      else:
        return False

  def checkForSharedAlbum(self,keyword):
    logging.debug('checking shared albums.')
    url = 'https://photoslibrary.googleapis.com/v1/sharedAlbums'
    data = self.oauth.request(url).json()
    albumid = None
    for i in range(len(data['sharedAlbums'])):
      if 'title' in data['sharedAlbums'][i] and data['sharedAlbums'][i]['title'] == keyword:
        albumid = data['sharedAlbums'][i]['id']
    if albumid is None:
      logging.info('Could not find shared album named %s' % keyword)
    return albumid
  
  def checkForOwnAlbum(self,keyword):
    logging.debug('checking own albums.')
    url = 'https://photoslibrary.googleapis.com/v1/albums'
    data = self.oauth.request(url).json()
    albumid = None
    for i in range(len(data['albums'])):
      if 'title' in data['albums'][i] and data['albums'][i]['title'] == keyword:
        albumid = data['albums'][i]['id']
    if albumid is None:
      logging.info('Could not find own album named %s' % keyword)
    return albumid

  def getPhotoList(self,albumid):
    picturecount = self.settings.getUser('picturecount')
    mediaItems = []
    params = {}
    if albumid is None:
      logging.info('downloading latest photos..')
      endDate = datetime.date.today()
      startDate = endDate - datetime.timedelta(days=60)
      params = {
          'pageSize' : 100,
          'filters': {
            'dateFilter':{
              'ranges':[{
                'startDate':{
                  "year": startDate.year,
                  "month": startDate.month,
                  "day": startDate.day
                },
                'endDate':{
                  "year": endDate.year,
                  "month": endDate.month,
                  "day": endDate.day
                }
              }]
            },
            'mediaTypeFilter': {
              'mediaTypes': [
                'PHOTO'
              ]
            }
          }
        }
    else:
      logging.info('Downloading pictures from the album')
      params = {
          'albumId' : albumid,
          'pageSize' : 100,
        }
    if picturecount <= 100:
      params['pageSize'] = picturecount
    # Request albums      
    url = 'https://photoslibrary.googleapis.com/v1/mediaItems:search'
    #logging.debug('Downloading image list for %s...' % keyword)
    data = self.oauth.request(url, params=params,post=True).json()
    logging.debug('received %d photo ids. Looking for %d pictures' % (len(data['mediaItems']),picturecount))
    mediaItems = data['mediaItems']
    while 'nextPageToken' in data and len(mediaItems) < picturecount:
      params['pageToken'] = data['nextPageToken']
      data = self.oauth.request(url, params=params,post=True)
      logging.debug('Data code == %d' % data.status_code)
      if data.status_code == 200:
        data = data.json()
        logging.debug('received %d photo ids.' % len(data['mediaItems']))
        mediaItems = mediaItems + data['mediaItems']
      else:
        break
    logging.debug('found %d photo ids overall.' % len(mediaItems))
    return mediaItems

