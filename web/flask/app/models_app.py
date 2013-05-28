from app import app, db
import models
from modules.packages_prefixes import packages_prefixes
from modules.sourcecode import SourceCodeIterator

from flask import url_for

import os, subprocess, re

class Package_app(models.Package, db.Model):
    @staticmethod
    def get_packages_prefixes():
        """
        returns the packages prefixes (a, b, ..., liba, libb, ..., y, z)
        """
        return packages_prefixes

class Version_app(models.Version, db.Model):
    pass

class Location(object):
    """ a location in a package, can be a directory or a file """
    def __init__(self, package, version=None, path_to=None):
        self.package = package
        self.version = version or ""
        self.path_to = path_to or ""
        
        # we wanna list the package versions in each of main/contrib/nonfree
        self.sources_path = os.path.join(app.config['SOURCES_FOLDER'],
                                         "main", "h", # TODO
                                         self.package, self.version,
                                         self.path_to)
        self.sources_path_raw = os.path.join(app.config['SOURCES_RAW'],
                                             "main", "h", # TODO
                                             self.package, self.version,
                                             self.path_to)
    
    def isdir(self):
        """ True if self is a directory, False if it's not """
        return os.path.isdir(self.sources_path)
    
    def isfile(self):
        """ True if sels is a file, False if it's not """
        return os.path.isfile(self.sources_path)
    
    def istextfile(self):
        """ 
        True if self is a text file, False if it's not.
        Based on the UNIX command 'file' result, also doesn't work elsewhere
        """
        mime = subprocess.Popen(["file", self.sources_path],
                                stdout=subprocess.PIPE).communicate()[0]
        return re.search('text', mime) != None

    def get_raw_url(self):
        return self.sources_path_raw
    
    def get_path_links(self):
        """
        returns the path hierarchy with urls, to use with 'You are here:'
        [(name, url(name)), (...), ...]
        """
        pathl = []
        pathl.append((self.package, url_for('source', package=self.package)))
        
        if self.version != "":
            pathl.append((self.version, url_for('source', package=self.package,
                                                version=self.version)))
        if self.path_to != "":
            prev_path = ""
            for p in self.path_to.split('/'):
                pathl.append((p, url_for('source', package=self.package,
                                         version=self.version,
                                         path_to=prev_path+p)))
                prev_path += p+"/"
        return pathl

class Directory(Location):
    """ a folder in a package """
    def _sub_url(self, subfile):
        """ returns the URL of a sub file/folder in this directory """
        if self.version == "":
            return url_for('source', package=self.package, version=subfile)
        elif self.path_to == "":
            return url_for('source', package=self.package,
                           version=self.version, path_to=subfile)
        else:
            return url_for('source', package=self.package,
                           version=self.version,
                           path_to=self.path_to+"/"+subfile)
    
    def get_subdirs(self):
        """ returns the list of the subfolders along with their URLs """
        return sorted((f, self._sub_url(f))
                      for f in os.listdir(self.sources_path)
                      if os.path.isdir(os.path.join(self.sources_path, f)))
    
    def get_subfiles(self):
        """ returns the list of the subfiles along with their URLs """
        return sorted((d, self._sub_url(d))
                      for d in os.listdir(self.sources_path)
                      if os.path.isfile(os.path.join(self.sources_path, d)))
    
    def is_top_folder(self):
        """ True if this is a top folder of a package, False otherwise """
        return self.version == ""

class SourceFile(Location):
    """ a source file in a package """
    def __init__(self, package, version, path_to, highlight, msg):
        super(SourceFile, self).__init__(package, version, path_to)
        self.highlight = highlight
        self.msg = msg
        self.number_of_lines = None
        self.code = SourceCodeIterator(self.sources_path, self.highlight)
    
    def get_msgdict(self):
        """
        returns a dict(position=, title=, message=) generated from
        the string message (position:title:message)
        """
        if self.msg is None: return dict()
        msgsplit = self.msg.split(':')
        msgdict = dict()
        try:
            msgdict['position'] = int(msgsplit[0])
        except ValueError:
            msgdict['position'] = 1
        try:
            msgdict['title'] = msgsplit[1]
        except IndexError:
            msgdict['title'] = ""
        try:
            msgdict['message'] = ":".join(msgsplit[2:])
        except IndexError:
            msgdict['message'] = ""
        return msgdict
    
    def get_number_of_lines(self):
        if self.number_of_lines is not None:
            return number_of_lines
        number_of_lines = 0
        with open(self.sources_path) as sfile:
            for line in sfile: number_of_lines += 1
        return number_of_lines
    
    def get_code(self):
        return self.code

