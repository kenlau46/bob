# Bob build tool
# Copyright (C) 2016  Jan Klötzke
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from .errors import BuildError
from .tty import colorize
from .utils import asHexStr, removePath
from tempfile import mkstemp, TemporaryFile
from pipes import quote
import os.path
import subprocess
import tarfile
import textwrap
import urllib.request, urllib.error

class DummyArchive:
    """Archive that does nothing"""

    def wantDownload(self, enable):
        pass

    def wantUpload(self, enable):
        pass

    def canDownload(self):
        return False

    def canUpload(self):
        return False

    def uploadPackage(self, buildId, path):
        pass

    def downloadPackage(self, buildId, path):
        return False

    def upload(self, step, buildIdFile, tgzFile):
        return ""

    def download(self, step, buildIdFile, tgzFile):
        return ""

class BaseArchive:
    def __init__(self, spec):
        use = spec.get("flags", ["upload", "download"])
        self.__useDownload = "download" in use
        self.__useUpload = "upload" in use
        self.__wantDownload = False
        self.__wantUpload = False

    def wantDownload(self, enable):
        self.__wantDownload = enable

    def wantUpload(self, enable):
        self.__wantUpload = enable

    def canDownload(self):
        return self.__wantDownload and self.__useDownload

    def canUpload(self):
        return self.__wantUpload and self.__useUpload


class LocalArchive(BaseArchive):
    def __init__(self, spec):
        super().__init__(spec)
        self.__basePath = os.path.abspath(spec["path"])

    def uploadPackage(self, buildId, path):
        if not self.canUpload():
            return

        packageResultId = asHexStr(buildId)
        packageResultPath = os.path.join(self.__basePath, packageResultId[0:2],
                                         packageResultId[2:4])
        packageResultFile = os.path.join(packageResultPath,
                                         packageResultId[4:]) + ".tgz"
        if os.path.isfile(packageResultFile):
            print("   UPLOAD    skipped ({} exists in archive)".format(path))
            return

        print(colorize("   UPLOAD    {}".format(path), "32"))
        if not os.path.isdir(packageResultPath): os.makedirs(packageResultPath)
        with tarfile.open(packageResultFile, "w:gz") as tar:
            tar.add(path, arcname=".")

    def downloadPackage(self, buildId, path):
        if not self.canDownload():
            return False

        print(colorize("   DOWNLOAD  {}...".format(path), "32"), end="")
        packageResultId = asHexStr(buildId)
        packageResultPath = os.path.join(self.__basePath, packageResultId[0:2],
                                         packageResultId[2:4])
        packageResultFile = os.path.join(packageResultPath,
                                         packageResultId[4:]) + ".tgz"
        if os.path.isfile(packageResultFile):
            removePath(path)
            os.makedirs(path)
            with tarfile.open(packageResultFile, "r:gz") as tar:
                tar.extractall(path)
            print(colorize("ok", "32"))
            return True
        else:
            print(colorize("not found", "33"))
            return False

    def upload(self, step, buildIdFile, tgzFile):
        if self.canUpload():
            return ""

        return "\n" + textwrap.dedent("""\
            # upload artifact
            cd $WORKSPACE
            BOB_UPLOAD_FILE="{DIR}/$(hexdump -e '2/1 "%02x/" 14/1 "%02x"' {BUILDID}).tgz"
            if [[ ! -e ${{BOB_UPLOAD_FILE}} ]] ; then
                mkdir -p "${{BOB_UPLOAD_FILE%/*}}"
                cp {RESULT} "$BOB_UPLOAD_FILE"
            fi""".format(DIR=self.__basePath, BUILDID=quote(buildIdFile), RESULT=quote(tgzFile)))

    def download(self, step, buildIdFile, tgzFile):
        if not self.canDownload():
            return ""

        return "\n" + textwrap.dedent("""\
            if [[ ! -e {RESULT} ]] ; then
                BOB_DOWNLOAD_FILE="{DIR}/$(hexdump -e '2/1 "%02x/" 14/1 "%02x"' {BUILDID}).tgz"
                cp "$BOB_DOWNLOAD_FILE" {RESULT} || echo Download failed: $?
            fi
            """.format(DIR=self.__basePath, BUILDID=quote(buildIdFile), RESULT=quote(tgzFile)))


class SimpleHttpArchive(BaseArchive):
    def __init__(self, spec):
        super().__init__(spec)
        self.__url = spec["url"]

    def _makeUrl(self, buildId):
        packageResultId = asHexStr(buildId)
        return "/".join([self.__url, packageResultId[0:2], packageResultId[2:4],
            packageResultId[4:] + ".tgz"])

    def uploadPackage(self, buildId, path):
        if not self.canUpload():
            return

        # check if already there
        url = self._makeUrl(buildId)
        try:
            try:
                req = urllib.request.Request(url=url, method='HEAD')
                f = urllib.request.urlopen(req)
                print("   UPLOAD    skipped ({} exists in archive)".format(path))
                return
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise BuildError("Error for HEAD on "+url+": "+e.reason)

            print(colorize("   UPLOAD    {}".format(path), "32"))
            with TemporaryFile() as tmpFile:
                with tarfile.open(fileobj=tmpFile, mode="w:gz") as tar:
                    tar.add(path, arcname=".")
                tmpFile.seek(0)
                req = urllib.request.Request(url=url, data=tmpFile.read(),
                                             method='PUT')
                f = urllib.request.urlopen(req)
        except urllib.error.URLError as e:
            raise BuildError("Error uploading package: "+str(e.reason))

    def downloadPackage(self, buildId, path):
        if not self.canDownload():
            return False

        ret = False
        print(colorize("   DOWNLOAD  {}...".format(path), "32"), end="")
        url = self._makeUrl(buildId)
        try:
            (localFilename, headers) = urllib.request.urlretrieve(url)
            removePath(path)
            os.makedirs(path)
            with tarfile.open(localFilename, "r:gz", errorlevel=1) as tar:
                tar.extractall(path)
            ret = True
            print(colorize("ok", "32"))
        except urllib.error.URLError as e:
            print(colorize(str(e.reason), "33"))
        except OSError as e:
            raise BuildError("Error: " + str(e))
        finally:
            urllib.request.urlcleanup()

        return ret

    def upload(self, step, buildIdFile, tgzFile):
        # only upload if requested
        if not self.canUpload():
            return ""

        # upload with curl if file does not exist yet on server
        return "\n" + textwrap.dedent("""\
            # upload artifact
            cd $WORKSPACE
            BOB_UPLOAD_URL="{URL}/$(hexdump -e '2/1 "%02x/" 14/1 "%02x"' {BUILDID}).tgz"
            if ! curl --output /dev/null --silent --head --fail "$BOB_UPLOAD_URL" ; then
                curl -sSg -T {RESULT} "$BOB_UPLOAD_URL" || echo Upload failed: $?
            fi""".format(URL=self.__url, BUILDID=quote(buildIdFile), RESULT=quote(tgzFile)))

    def download(self, step, buildIdFile, tgzFile):
        # only download if requested
        if not self.canDownload():
            return ""

        return "\n" + textwrap.dedent("""\
            if [[ ! -e {RESULT} ]] ; then
                BOB_DOWNLOAD_URL="{URL}/$(hexdump -e '2/1 "%02x/" 14/1 "%02x"' {BUILDID}).tgz"
                curl -sSg --fail -o {RESULT} "$BOB_DOWNLOAD_URL" || echo Download failed: $?
            fi
            """.format(URL=self.__url, BUILDID=quote(buildIdFile), RESULT=quote(tgzFile)))


class CustomArchive(BaseArchive):
    """Custom command archive"""

    def __init__(self, spec, whiteList):
        super().__init__(spec)
        self.__downloadCmd = spec.get("download")
        self.__uploadCmd = spec.get("upload")
        self.__whiteList = whiteList

    def _makeUrl(self, buildId):
        packageResultId = asHexStr(buildId)
        return "/".join([packageResultId[0:2], packageResultId[2:4],
            packageResultId[4:] + ".tgz"])

    def canDownload(self):
        return super().canDownload() and (self.__downloadCmd is not None)

    def canUpload(self):
        return super().canUpload() and (self.__uploadCmd is not None)

    def uploadPackage(self, buildId, path):
        if not self.canUpload():
            return

        print(colorize("   UPLOAD    {}".format(path), "32"))
        (tmpFd, tmpName) = mkstemp()
        try:
            os.close(tmpFd)
            with tarfile.open(tmpName, mode="w:gz") as tar:
                tar.add(path, arcname=".")

            env = { k:v for (k,v) in os.environ.items() if k in self.__whiteList }
            env["BOB_LOCAL_ARTIFACT"] = tmpName
            env["BOB_REMOTE_ARTIFACT"] = self._makeUrl(buildId)
            ret = subprocess.call(["/bin/bash", "-ec", self.__uploadCmd],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                cwd="/tmp", env=env)
            if ret != 0:
                raise BuildError("Upload failed: command return with status {}"
                                    .format(ret))
        except OSError as e:
            raise BuildError("Upload failed: " + str(e))
        finally:
            os.unlink(tmpName)

    def downloadPackage(self, buildId, path):
        if not self.canDownload():
            return False

        success = False
        print(colorize("   DOWNLOAD  {}...".format(path), "32"), end="")
        (tmpFd, tmpName) = mkstemp()
        try:
            os.close(tmpFd)
            env = { k:v for (k,v) in os.environ.items() if k in self.__whiteList }
            env["BOB_LOCAL_ARTIFACT"] = tmpName
            env["BOB_REMOTE_ARTIFACT"] = self._makeUrl(buildId)
            ret = subprocess.call(["/bin/bash", "-ec", self.__downloadCmd],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                cwd="/tmp", env=env)
            if ret == 0:
                removePath(path)
                os.makedirs(path)
                with tarfile.open(tmpName, "r:gz", errorlevel=1) as tar:
                    tar.extractall(path)
                print(colorize("ok", "32"))
                success = True
            else:
                print(colorize("failed (exit {})".format(ret), "33"))
        except OSError as e:
            raise BuildError("Download failed: " + str(e))
        finally:
            os.unlink(tmpName)

        return success

    def upload(self, step, buildIdFile, tgzFile):
        # only upload if requested
        if not self.canUpload():
            return ""

        return "\n" + textwrap.dedent("""\
            # upload artifact
            cd $WORKSPACE
            BOB_UPLOAD_BID="$(hexdump -ve '/1 "%02x"' {BUILDID})"
            BOB_LOCAL_ARTIFACT={RESULT}
            BOB_REMOTE_ARTIFACT="${{BOB_UPLOAD_BID:0:2}}/${{BOB_UPLOAD_BID:2:2}}/${{BOB_UPLOAD_BID:4}}.tgz"
            {CMD}
            """.format(CMD=self.__uploadCmd, BUILDID=quote(buildIdFile), RESULT=quote(tgzFile)))

    def download(self, step, buildIdFile, tgzFile):
        # only download if requested
        if not self.canDownload():
            return ""

        return "\n" + textwrap.dedent("""\
            if [[ ! -e {RESULT} ]] ; then
                BOB_DOWNLOAD_BID="$(hexdump -ve '/1 "%02x"' {BUILDID})"
                BOB_LOCAL_ARTIFACT={RESULT}
                BOB_REMOTE_ARTIFACT="${{BOB_DOWNLOAD_BID:0:2}}/${{BOB_DOWNLOAD_BID:2:2}}/${{BOB_DOWNLOAD_BID:4}}.tgz"
                {CMD}
            fi
            """.format(CMD=self.__downloadCmd, BUILDID=quote(buildIdFile), RESULT=quote(tgzFile)))


class MultiArchive:
    def __init__(self, archives):
        self.__archives = archives

    def wantDownload(self, enable):
        for i in self.__archives: i.wantDownload(enable)

    def wantUpload(self, enable):
        for i in self.__archives: i.wantUpload(enable)

    def canDownload(self):
        return any(i.canDownload() for i in self.__archives)

    def canUpload(self):
        return any(i.canUpload() for i in self.__archives)

    def uploadPackage(self, buildId, path):
        for i in self.__archives:
            if not i.canUpload(): continue
            i.uploadPackage(buildId, path)

    def downloadPackage(self, buildId, path):
        for i in self.__archives:
            if not i.canDownload(): continue
            if i.downloadPackage(buildId, path): return True
        return False

    def upload(self, step, buildIdFile, tgzFile):
        return "\n".join(
            i.upload(step, buildIdFile, tgzFile) for i in self.__archives
            if i.canUpload())

    def download(self, step, buildIdFile, tgzFile):
        return "\n".join(
            i.download(step, buildIdFile, tgzFile) for i in self.__archives
            if i.canDownload())


def getSingleArchiver(recipes, archiveSpec):
    archiveBackend = archiveSpec.get("backend", "none")
    if archiveBackend == "file":
        return LocalArchive(archiveSpec)
    elif archiveBackend == "http":
        return SimpleHttpArchive(archiveSpec)
    elif archiveBackend == "shell":
        return CustomArchive(archiveSpec, recipes.envWhiteList())
    elif archiveBackend == "none":
        return DummyArchive()
    else:
        raise BuildError("Invalid archive backend: "+archiveBackend)

def getArchiver(recipes):
    archiveSpec = recipes.archiveSpec()
    if isinstance(archiveSpec, list):
        return MultiArchive([ getSingleArchiver(recipes, i) for i in archiveSpec ])
    else:
        return getSingleArchiver(recipes, archiveSpec)

