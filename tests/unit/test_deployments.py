# Copyright 2021 Northern.tech AS
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

import hashlib
import os

import pytest
import requests
from werkzeug.wrappers import Response

from mender.client import deployments


class CustomContentLengthResponse(Response):
    automatically_set_content_length = False


class TestDeploymentsMisc:
    def test_exponential_backoff_time(self):
        # Test with one minute maximum interval.
        intvl = deployments.get_exponential_backoff_time(0, 1 * 60)
        assert intvl == 1 * 60

        intvl = deployments.get_exponential_backoff_time(1, 1 * 60)
        assert intvl == 1 * 60

        intvl = deployments.get_exponential_backoff_time(2, 1 * 60)
        assert intvl == 1 * 60

        with pytest.raises(deployments.DeploymentDownloadFailed):
            intvl = deployments.get_exponential_backoff_time(3, 1 * 60)

        with pytest.raises(deployments.DeploymentDownloadFailed):
            intvl = deployments.get_exponential_backoff_time(7, 1 * 60)

        # Test with two minute maximum interval.
        intvl = deployments.get_exponential_backoff_time(5, 2 * 60)
        assert intvl == 2 * 60

        with pytest.raises(deployments.DeploymentDownloadFailed):
            intvl = deployments.get_exponential_backoff_time(6, 2 * 60)

        # Test with 10 minute maximum interval.
        intvl = deployments.get_exponential_backoff_time(11, 10 * 60)
        assert intvl == 8 * 60

        intvl = deployments.get_exponential_backoff_time(12, 10 * 60)
        assert intvl == 10 * 60

        intvl = deployments.get_exponential_backoff_time(14, 10 * 60)
        assert intvl == 10 * 60

        with pytest.raises(deployments.DeploymentDownloadFailed):
            intvl = deployments.get_exponential_backoff_time(15, 10 * 60)

        # Test with one second maximum interval.
        intvl = deployments.get_exponential_backoff_time(0, 1)
        assert intvl == 1 * 60

        intvl = deployments.get_exponential_backoff_time(1, 1)
        assert intvl == 1 * 60

        intvl = deployments.get_exponential_backoff_time(2, 1)
        assert intvl == 1 * 60

        with pytest.raises(deployments.DeploymentDownloadFailed):
            intvl = deployments.get_exponential_backoff_time(3, 1)

    def test_header_range_regex(self):
        m = deployments.header_range_regex.match("bytes 123-456/789")
        assert m is not None
        assert m.group(1) == "123"
        assert m.group(2) == "456"
        assert m.group(3) == "789"

        m = deployments.header_range_regex.match("bytes 123-456/*")
        assert m is not None
        assert m.group(1) == "123"
        assert m.group(2) == "456"
        assert m.group(3) is None

        m = deployments.header_range_regex.match("bites 123-456/789")
        assert m is None
        m = deployments.header_range_regex.match("bytes 1a23-456/789")
        assert m is None
        m = deployments.header_range_regex.match("bites 123/456/789")
        assert m is None


@pytest.fixture(scope="class")
def tweak_download_resume_intervals(request):
    orig_download_resume_min_interval = deployments.DOWNLOAD_RESUME_MIN_INTERVAL_SECONDS
    orig_download_resume_max_interval = deployments.DOWNLOAD_RESUME_MAX_INTERVAL_SECONDS

    deployments.DOWNLOAD_RESUME_MIN_INTERVAL_SECONDS = 2
    deployments.DOWNLOAD_RESUME_MAX_INTERVAL_SECONDS = 5

    def fin():
        deployments.DOWNLOAD_RESUME_MIN_INTERVAL = orig_download_resume_min_interval
        deployments.DOWNLOAD_RESUME_MAX_INTERVAL = orig_download_resume_max_interval

    request.addfinalizer(fin)


@pytest.mark.usefixtures("tweak_download_resume_intervals")
class TestDeploymentsDownloadAndResume:

    raw_data_len = 10 * 1024 * 1024  # 10 MiB
    raw_data = bytearray(os.urandom(10 * 1024 * 1024))

    def do_run_test(self, httpserver):
        d_dict = {
            "id": "dummy",
            "artifact": {
                "artifact_name": "test",
                "source": {"uri": httpserver.url_for("/foobar")},
            },
        }

        d_info = deployments.DeploymentInfo(d_dict)

        assert deployments.download_and_resume(d_info, "/tmp/dummy", "")

        with open("/tmp/dummy", "rb") as fh:
            read_back = fh.read()

        assert (
            hashlib.sha256(self.raw_data).hexdigest()
            == hashlib.sha256(read_back).hexdigest()
        )

    def test_download_in_one_go(self, httpserver):
        httpserver.expect_request("/foobar").respond_with_data(
            self.raw_data, status=requests.codes.OK
        )

        self.do_run_test(httpserver)

    def test_no_range_support(self, httpserver):
        def h_partial_response(_):
            headers = {"Content-Length": self.raw_data_len}
            return CustomContentLengthResponse(
                self.raw_data[: 2 * 1024 * 1024],
                status=requests.codes.OK,
                headers=headers,
            )

        def h_full_response(_):
            return Response(self.raw_data, status=requests.codes.OK)

        httpserver.expect_ordered_request("/foobar").respond_with_handler(
            h_partial_response
        )
        httpserver.expect_ordered_request("/foobar").respond_with_handler(
            h_full_response
        )

        self.do_run_test(httpserver)

    def test_yes_range_support(self, httpserver):
        partial_data_len = int(self.raw_data_len * 20 / 100)

        def h_partial_response(_):
            headers = {"Content-Length": self.raw_data_len}
            return CustomContentLengthResponse(
                self.raw_data[:partial_data_len],
                status=requests.codes.OK,
                headers=headers,
            )

        def h_resume_response(request):
            range_hdr = request.headers.get("Range")
            assert range_hdr == "bytes=%d-" % partial_data_len
            headers = {
                "Content-Range": "bytes %d-%d/*"
                % (partial_data_len, self.raw_data_len - 1)
            }
            return Response(
                self.raw_data[partial_data_len:],
                status=requests.codes.PARTIAL_CONTENT,
                headers=headers,
            )

        httpserver.expect_ordered_request("/foobar").respond_with_handler(
            h_partial_response
        )
        httpserver.expect_ordered_request("/foobar").respond_with_handler(
            h_resume_response
        )

        self.do_run_test(httpserver)

    def test_range_early_start(self, httpserver):
        partial_data_len = int(self.raw_data_len * 20 / 100)

        def h_partial_response(_):
            headers = {"Content-Length": self.raw_data_len}
            return CustomContentLengthResponse(
                self.raw_data[:partial_data_len],
                status=requests.codes.OK,
                headers=headers,
            )

        def h_resume_response(request):
            range_hdr = request.headers.get("Range")
            assert range_hdr == "bytes=%d-" % partial_data_len
            headers = {
                "Content-Range": "bytes %d-%d/*" % (512 * 1024, self.raw_data_len - 1)
            }
            return Response(
                self.raw_data[512 * 1024 :],
                status=requests.codes.PARTIAL_CONTENT,
                headers=headers,
            )

        httpserver.expect_ordered_request("/foobar").respond_with_handler(
            h_partial_response
        )
        httpserver.expect_ordered_request("/foobar").respond_with_handler(
            h_resume_response
        )

        self.do_run_test(httpserver)

    def test_range_late_start(self, httpserver):
        partial_data_len = int(self.raw_data_len * 20 / 100)

        def h_partial_response(_):
            headers = {"Content-Length": self.raw_data_len}
            return CustomContentLengthResponse(
                self.raw_data[:partial_data_len],
                status=requests.codes.OK,
                headers=headers,
            )

        def h_resume_response(request):
            range_hdr = request.headers.get("Range")
            assert range_hdr == "bytes=%d-" % partial_data_len
            headers = {
                "Content-Range": "bytes %d-%d/*"
                % (partial_data_len + 10, self.raw_data_len - 1)
            }
            return Response(
                self.raw_data[partial_data_len + 10 :],
                status=requests.codes.PARTIAL_CONTENT,
                headers=headers,
            )

        httpserver.expect_ordered_request("/foobar").respond_with_handler(
            h_partial_response
        )
        httpserver.expect_ordered_request("/foobar").respond_with_handler(
            h_resume_response
        )
        httpserver.expect_request("/foobar").respond_with_data(
            bytes(), status=requests.codes.OK
        )

        with pytest.raises(deployments.DeploymentDownloadFailed):
            self.do_run_test(httpserver)

    def test_broken_content_length(self, httpserver):
        partial_data_len = int(self.raw_data_len * 20 / 100)

        def h_partial_response(_):
            headers = {"Content-Length": self.raw_data_len}
            return CustomContentLengthResponse(
                self.raw_data[:partial_data_len],
                status=requests.codes.OK,
                headers=headers,
            )

        def h_resume_response(request):
            range_hdr = request.headers.get("Range")
            assert range_hdr == "bytes=%d-" % partial_data_len
            headers = {
                "Content-Range": "bytes %d-%d/*"
                % (partial_data_len + 10, self.raw_data_len - 2),
                "Content-Length": self.raw_data_len - partial_data_len - 10 - 1,
            }
            return CustomContentLengthResponse(
                self.raw_data[partial_data_len + 10 :],
                status=requests.codes.PARTIAL_CONTENT,
                headers=headers,
            )

        httpserver.expect_ordered_request("/foobar").respond_with_handler(
            h_partial_response
        )
        httpserver.expect_ordered_request("/foobar").respond_with_handler(
            h_resume_response
        )
        httpserver.expect_request("/foobar").respond_with_data(
            bytes(), status=requests.codes.OK
        )

        with pytest.raises(deployments.DeploymentDownloadFailed):
            self.do_run_test(httpserver)

    def test_bogus_content_range(self, httpserver):
        partial_data_len = int(self.raw_data_len * 20 / 100)

        def h_partial_response(_):
            headers = {"Content-Length": self.raw_data_len}
            return CustomContentLengthResponse(
                self.raw_data[:partial_data_len],
                status=requests.codes.OK,
                headers=headers,
            )

        def h_resume_response(request):
            range_hdr = request.headers.get("Range")
            assert range_hdr == "bytes=%d-" % partial_data_len
            headers = {"Content-Range": "bytes abcd-efgh/ijkl"}
            return Response(
                self.raw_data[partial_data_len:],
                status=requests.codes.PARTIAL_CONTENT,
                headers=headers,
            )

        httpserver.expect_ordered_request("/foobar").respond_with_handler(
            h_partial_response
        )
        httpserver.expect_ordered_request("/foobar").respond_with_handler(
            h_resume_response
        )

        with pytest.raises(deployments.DeploymentDownloadFailed):
            self.do_run_test(httpserver)
