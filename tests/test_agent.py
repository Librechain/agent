import asyncio
import datetime
import json
from unittest import mock
from pathlib import Path
import time

import pytest
import freezegun

import agent
from agent.journal_helper import logins_last_hour
from agent.os_helper import detect_raspberry_pi, kernel_cmdline
from agent.iptables_helper import block_networks, block_ports, OUTPUT_CHAIN, INPUT_CHAIN
from agent.security_helper import check_for_default_passwords, selinux_status
from agent import executor
import pwd
from os import getenv


def test_detect_raspberry_pi():
    class mockPath():
        def __init__(self, filename):
            self._filename = filename

        def is_file(self):
            return True

        def open(self):
            return mock_open(self._filename)

    def mock_open(filename, mode='r'):
        """
        This will return either a Unicode string needed for "r" mode or bytes for "rb" mode.
        The contents are still the same which is the mock sshd_config. But they are only interpreted
        by audit_sshd.
        """
        if filename == '/proc/device-tree/model':
            content = 'Raspberry Pi 3 Model B Plus Rev 1.3\x00'
        elif filename == '/proc/device-tree/serial-number':
            content = '0000000060e3b222\x00'
        else:
            raise FileNotFoundError
        file_object = mock.mock_open(read_data=content).return_value
        file_object.__iter__.return_value = content.splitlines(True)
        return file_object

    with mock.patch('agent.os_helper.Path', mockPath):
        metadata = detect_raspberry_pi()
        assert metadata['is_raspberry_pi']
        assert metadata['hardware_model'] == 'Raspberry Pi 3 Model B Plus Rev 1.3'
        assert metadata['serial_number'] == '0000000060e3b222'


def test_failed_logins():
    with mock.patch('agent.journal_helper.get_journal_records') as gjr:
        gjr.return_value = [
        ]
        result = logins_last_hour()
        assert result == {}

    with mock.patch('agent.journal_helper.get_journal_records') as gjr:
        gjr.return_value = [
            {'MESSAGE': 'pam_unix(sshd:auth): authentication failure; logname= uid=0 euid=0 tty=ssh ruser= rhost=10.147.17.225'}
        ]
        result = logins_last_hour()
        assert result == {'': {'success': 0, 'failed': 1}}

    with mock.patch('agent.journal_helper.get_journal_records') as gjr:
        gjr.return_value = [
            {'MESSAGE': 'pam_unix(sshd:auth): authentication failure; logname= uid=0 euid=0 tty=ssh ruser= rhost=10.147.17.225  user=pi'}
        ]
        result = logins_last_hour()
        assert result == {'pi': {'success': 0, 'failed': 1}}

    with mock.patch('agent.journal_helper.get_journal_records') as gjr:
        gjr.return_value = [
            {'MESSAGE': 'PAM 2 more authentication failures; logname= uid=0 euid=0 tty=ssh ruser= rhost=10.147.17.225  user=pi'},
            {'MESSAGE': 'pam_unix(sshd:auth): authentication failure; logname= uid=0 euid=0 tty=ssh ruser= rhost=10.147.17.225  user=pi'},
            {'MESSAGE': 'PAM 1 more authentication failure; logname= uid=0 euid=0 tty=ssh ruser= rhost=10.147.17.225  user=pi'},
            {'MESSAGE': 'pam_unix(sshd:session): session opened for user pi by (uid=0)'},
            {'MESSAGE': 'pam_unix(sshd:auth): authentication failure; logname= uid=0 euid=0 tty=ssh ruser= rhost=10.147.17.225'}
        ]
        result = logins_last_hour()
        assert result == {
            'pi': {'success': 1, 'failed': 4},
            '': {'success': 0, 'failed': 1}}

    with mock.patch('agent.journal_helper.get_journal_records') as gjr:
        gjr.return_value = [
            {'MESSAGE': 'pam_unix(sshd:auth): some other message'},
            {'MESSAGE': 'something unrelated'},
            {'MESSAGE': 'PAM and something unrelated'},
        ]
        result = logins_last_hour()
        assert result == {}


def test_is_bootstrapping_stat_file(tmpdir):
    agent.CERT_PATH = str(tmpdir)
    agent.CLIENT_CERT_PATH = str(tmpdir / 'client.crt')
    with mock.patch('agent.logger') as prn:
        assert agent.is_bootstrapping()
        assert mock.call('No certificate found on disk.') in prn.warning.mock_calls


def test_is_bootstrapping_create_dir(tmpdir):
    notexistent_dir = tmpdir / 'notexistent'
    agent.CERT_PATH = str(notexistent_dir)
    agent.CLIENT_CERT_PATH = str(notexistent_dir / 'client.crt')
    with mock.patch('os.makedirs') as md, \
            mock.patch('os.chmod') as chm, \
            mock.patch('agent.logger') as prn:
        assert agent.is_bootstrapping()
        assert md.called_with(notexistent_dir)
        assert chm.called_with(notexistent_dir, 0o700)
        assert mock.call('No certificate found on disk.') in prn.warning.mock_calls


def test_is_bootstrapping_check_filesize(tmpdir):
    crt = tmpdir / 'client.crt'
    agent.CERT_PATH = str(tmpdir)
    agent.CLIENT_CERT_PATH = str(crt)
    with mock.patch('agent.logger') as prn:
        Path(agent.CLIENT_CERT_PATH).touch()
        assert agent.is_bootstrapping()
        assert mock.call('Certificate found but it is broken') in prn.warning.mock_calls


def test_is_bootstrapping_false_on_valid_cert(tmpdir):
    crt = tmpdir / 'client.crt'
    agent.CERT_PATH = str(tmpdir)
    agent.CLIENT_CERT_PATH = str(crt)
    with mock.patch('builtins.print') as prn:
        Path(agent.CLIENT_CERT_PATH).write_text('nonzero')
        assert not agent.is_bootstrapping()
        assert not prn.mock_calls


def test_can_read_cert_stat_cert(tmpdir):
    crt = tmpdir / 'client.crt'
    key = tmpdir / 'client.key'
    agent.CERT_PATH = str(tmpdir)
    agent.CLIENT_CERT_PATH = str(crt)
    agent.CLIENT_KEY_PATH = str(key)
    with mock.patch('agent.logger') as prn:
        # Path(crt).touch(mode=0o100)
        with pytest.raises(SystemExit):
            agent.can_read_cert()
        assert mock.call('Permission denied when trying to read the certificate file.') in prn.error.mock_calls


def test_can_read_cert_stat_key(tmpdir):
    crt = tmpdir / 'client.crt'
    key = tmpdir / 'client.key'
    agent.CERT_PATH = str(tmpdir)
    agent.CLIENT_CERT_PATH = str(crt)
    agent.CLIENT_KEY_PATH = str(key)
    with mock.patch('agent.logger') as prn:
        Path(agent.CLIENT_CERT_PATH).touch(mode=0o600)
        # Path(agent.CLIENT_KEY_PATH).touch(mode=0o100)
        with pytest.raises(SystemExit):
            agent.can_read_cert()
        assert mock.call('Permission denied when trying to read the key file.') in prn.error.mock_calls


def test_can_read_cert_none_on_success(tmpdir):
    crt = tmpdir / 'client.crt'
    key = tmpdir / 'client.key'
    agent.CERT_PATH = str(tmpdir)
    agent.CLIENT_CERT_PATH = str(crt)
    agent.CLIENT_KEY_PATH = str(key)
    with mock.patch('agent.logger'):
        Path(agent.CLIENT_CERT_PATH).touch(mode=0o600)
        Path(agent.CLIENT_KEY_PATH).touch(mode=0o600)
        can_read = agent.can_read_cert()
        assert can_read is None


def test_get_primary_ip(netif_gateways, netif_ifaddresses):
    with mock.patch('netifaces.gateways') as gw, \
            mock.patch('netifaces.ifaddresses') as ifaddr:
        gw.return_value = netif_gateways
        ifaddr.return_value = netif_ifaddresses
        primary_ip = agent.get_primary_ip()
        assert primary_ip == '192.168.1.3'


def test_get_primary_ip_none_on_exception(netif_gateways_invalid, netif_ifaddresses):
    with mock.patch('netifaces.gateways') as gw, \
            mock.patch('netifaces.ifaddresses') as ifaddr:
        gw.return_value = netif_gateways_invalid
        ifaddr.return_value = netif_ifaddresses
        primary_ip = agent.get_primary_ip()
        assert primary_ip is None


def test_get_certificate_expiration_date(cert):
    with mock.patch(
            'builtins.open',
            mock.mock_open(read_data=cert),
            create=True
    ):
        exp_date = agent.get_certificate_expiration_date()
        assert exp_date.date() == datetime.date(2019, 3, 19)


@freezegun.freeze_time("2019-04-04")
def test_time_for_certificate_renewal(cert):
    with mock.patch(
            'builtins.open',
            mock.mock_open(read_data=cert),
            create=True
    ):
        assert agent.time_for_certificate_renewal()


@freezegun.freeze_time("2019-04-14")
def test_cert_expired(cert):
    with mock.patch(
            'builtins.open',
            mock.mock_open(read_data=cert),
            create=True
    ), mock.patch('agent.can_read_cert') as cr:
        cr.return_value = True
        assert agent.is_certificate_expired()


@pytest.mark.vcr
def test_generate_device_id():
    dev_id = agent.generate_device_id()
    assert dev_id


def test_get_device_id(cert):
    with mock.patch(
            'builtins.open',
            mock.mock_open(read_data=cert),
            create=True
    ), mock.patch('agent.can_read_cert') as cr:
        cr.return_value = True
        device_id = agent.get_device_id()
        assert device_id == '4853b630822946019393b16c5b710b9e.d.wott.local'


def test_generate_cert():  # TODO: parse key and csr
    cert = agent.generate_cert('4853b630822946019393b16c5b710b9e.d.wott.local')
    assert cert['key']
    assert cert['csr']


@pytest.mark.vcr
def test_get_ca_cert():
    ca_bundle = agent.get_ca_cert()
    assert "BEGIN CERTIFICATE" in ca_bundle


def test_get_ca_cert_none_on_fail():
    with mock.patch('requests.get') as req, \
            mock.patch('agent.logger') as prn:
        req.return_value.ok = False
        ca_bundle = agent.get_ca_cert()
    assert ca_bundle is None
    assert mock.call('Failed to get CA...') in prn.error.mock_calls
    assert prn.error.call_count == 3


def test_get_open_ports(net_connections_fixture, netstat_result):
    with mock.patch('psutil.net_connections') as net_connections:
        net_connections.return_value = net_connections_fixture
        connections_ports = agent.get_open_ports()
        assert connections_ports == [netstat_result[1]]


@pytest.mark.vcr
def test_send_ping(raspberry_cpuinfo, uptime, tmpdir, cert, key, net_connections_fixture):
    crt_path = tmpdir / 'client.crt'
    key_path = tmpdir / 'client.key'
    agent.CERT_PATH = str(tmpdir)
    agent.CLIENT_CERT_PATH = str(crt_path)
    agent.CLIENT_KEY_PATH = str(key_path)
    Path(agent.CLIENT_CERT_PATH).write_text(cert)
    Path(agent.CLIENT_KEY_PATH).write_text(key)
    with mock.patch(
            'builtins.open',
            mock.mock_open(read_data=raspberry_cpuinfo),
            create=True
    ), \
    mock.patch('socket.getfqdn') as getfqdn, \
    mock.patch('psutil.net_connections') as net_connections, \
    mock.patch('agent.iptables_helper.dump') as fr, \
    mock.patch('agent.security_helper.check_for_default_passwords') as chdf, \
    mock.patch('agent.security_helper.process_scan') as ps, \
    mock.patch('agent.iptables_helper.block_ports') as bp, \
    mock.patch('agent.iptables_helper.block_networks') as bn, \
    mock.patch('agent.journal_helper.logins_last_hour') as logins, \
    mock.patch('builtins.print') as prn, \
    mock.patch(
        'builtins.open',
        mock.mock_open(read_data=uptime),
        create=True
    ):  # noqa E213
        net_connections.return_value = net_connections_fixture[0],
        fr.return_value = {}
        chdf.return_value = False
        ps.return_value = []
        getfqdn.return_value = 'localhost'
        bp.return_value = None
        bn.return_value = None
        logins.return_value = {}
        ping = agent.send_ping()
        assert ping is None
        assert prn.call_count == 0 or (prn.call_count == 1 and mock.call('Ping failed.') in prn.mock_calls)


@pytest.mark.vcr
def test_renew_cert(tmpdir, cert, key):
    crt_path = tmpdir / 'client.crt'
    key_path = tmpdir / 'client.key'
    agent.CERT_PATH = str(tmpdir)
    agent.CLIENT_CERT_PATH = str(crt_path)
    agent.CLIENT_KEY_PATH = str(key_path)
    Path(agent.CLIENT_CERT_PATH).write_text(cert)
    Path(agent.CLIENT_KEY_PATH).write_text(key)
    with mock.patch('socket.getfqdn') as getfqdn, \
            mock.patch('agent.logger') as prn:  # noqa E213
        getfqdn.return_value = 'localhost'
        res = agent.renew_expired_cert(None, None)
        assert res is None
        assert prn.info.call_count == 1
        assert prn.error.call_count == 1
        assert mock.call('Failed to submit CSR...') in prn.error.mock_calls


@pytest.mark.vcr
def test_say_hello_failed(tmpdir, invalid_cert, invalid_key):
    crt_path = tmpdir / 'client.crt'
    key_path = tmpdir / 'client.key'
    agent.CERT_PATH = str(tmpdir)
    agent.CLIENT_CERT_PATH = str(crt_path)
    agent.CLIENT_KEY_PATH = str(key_path)
    Path(agent.CLIENT_CERT_PATH).write_text(invalid_cert)
    Path(agent.CLIENT_KEY_PATH).write_text(invalid_key)
    with mock.patch('agent.logger') as prn:
        with pytest.raises(json.decoder.JSONDecodeError):
            _ = agent.say_hello()
        assert mock.call('Hello failed.') in prn.error.mock_calls


@pytest.mark.vcr
def test_say_hello_ok(tmpdir, cert, key):
    crt_path = tmpdir / 'client.crt'
    key_path = tmpdir / 'client.key'
    agent.CERT_PATH = str(tmpdir)
    agent.CLIENT_CERT_PATH = str(crt_path)
    agent.CLIENT_KEY_PATH = str(key_path)
    Path(agent.CLIENT_CERT_PATH).write_text(cert)
    Path(agent.CLIENT_KEY_PATH).write_text(key)
    hello = agent.say_hello()
    assert hello['message']


def test_uptime(uptime):
    with mock.patch(
            'builtins.open',
            mock.mock_open(read_data=uptime),
            create=True
    ):
        up = agent.get_uptime()
        assert up == 60


def test_check_for_default_passwords_pos():
    with mock.patch('pathlib.Path.open', mock.mock_open(read_data='pi:raspberry')),\
            mock.patch('spwd.getspall') as getspnam:
        # this is a real shadow record for password "raspberry"
        getspnam.return_value = [
            mock.Mock(
                sp_pwdp='$6$2tSrLNr4$XblkH.twWBJB.6zxbtyDM4z3Db55SOqdi3MBYPwNXF1Kv5FCGS6jCDdVNsr50kctHZk/W0u2AtyomcQ16EVZQ/',
                sp_namp='pi'
            )
        ]
        assert check_for_default_passwords('/doesntmatter/file.txt') == ['pi']


def test_check_for_default_passwords_neg():
    with mock.patch('pathlib.Path.open', mock.mock_open(read_data='pi:raspberry')),\
            mock.patch('spwd.getspall') as getspnam:
        # this is a real shadow record for password which is not "raspberry"
        getspnam.return_value = [
            mock.Mock(
                sp_pwdp='$6$/3W/.H6/$nncROMeVQxTEKRcjCfOwft08WPJm.JLnrlli0mutPZ737kImtHhcROgrYz7k6osr0XwuPDlwRfY.r584iQ425/',
                sp_namp='pi'
            )
        ]
        assert check_for_default_passwords('/doesntmatter/file.txt') == []


def test_audit_config_files(sshd_config):
    def mock_open(filename, mode='r'):
        """
        This will return either a Unicode string needed for "r" mode or bytes for "rb" mode.
        The contents are still the same which is the mock sshd_config. But they are only interpreted
        by audit_sshd.
        """
        if mode != 'rb':
            content = sshd_config
        else:
            content = sshd_config.encode()
        file_object = mock.mock_open(read_data=content).return_value
        file_object.__iter__.return_value = content.splitlines(True)
        return file_object
    with mock.patch('builtins.open',
                    new=mock_open),\
            mock.patch('os.path.isfile') as isfile,\
            mock.patch('os.path.getmtime') as getmtime:
        isfile.return_value = True
        getmtime.return_value = 0
        audit = agent.security_helper.audit_config_files()
        assert len(audit) == 4 and \
            audit[0]['sha256'] == audit[1]['sha256'] and \
            audit[0]['last_modified'] == 0 and \
            audit[3]['issues'] == {'PermitRootLogin': 'yes',
                                   'PasswordAuthentication': 'yes',
                                   'Protocol': '2,1',
                                   'AllowAgentForwarding': 'yes',
                                   'ClientAliveInterval': '0',
                                   'MaxAuthTries': '5'}


def test_block_networks(ipt_networks, ipt_rules):
    rule1, rule2 = ipt_rules
    net1, net2 = ipt_networks

    # Initial state: no networks are blocked
    # Input: two networks (net1, net2)
    # Result: net1 and net2 are blocked
    with mock.patch('agent.iptc_helper.batch_add_rules') as batch_add_rules:
        block_networks([net1, net2])
        batch_add_rules.assert_has_calls([
            mock.call('filter', [rule1, rule2], chain=OUTPUT_CHAIN, ipv6=False),
            mock.call('filter', [], chain=OUTPUT_CHAIN, ipv6=True),
        ])

    # Initial state: net1 is blocked
    # Input: another network: net2
    # Result: net2 gets blocked, net1 gets unblocked
    with mock.patch('agent.iptc_helper.batch_add_rules') as batch_add_rules:
        block_networks([net2])
        batch_add_rules.assert_has_calls([
            mock.call('filter', [rule2], chain=OUTPUT_CHAIN, ipv6=False),
            mock.call('filter', [], chain=OUTPUT_CHAIN, ipv6=True)
        ])

    # Initial state: empty
    # Input: empty
    # Result: nothing happens
    with mock.patch('agent.iptc_helper.batch_add_rules') as batch_add_rules:
        block_networks([])
        batch_add_rules.assert_has_calls([
            mock.call('filter', [], chain=OUTPUT_CHAIN, ipv6=False),
            mock.call('filter', [], chain=OUTPUT_CHAIN, ipv6=True)
        ])


def test_block_ports(ipt_ports, ipt_ports_rules):
    with mock.patch('agent.iptc_helper.batch_add_rules') as batch_add_rules:
        block_ports(True, ipt_ports)
        batch_add_rules.assert_has_calls([
            mock.call('filter', [r for r, ipv6 in ipt_ports_rules], chain=INPUT_CHAIN, ipv6=False)
        ])


def test_fetch_credentials(tmpdir):
    executor.Locker.LOCKDIR = str(tmpdir)
    agent.CREDENTIALS_PATH = str(tmpdir)
    json3_path_str = str(tmpdir / 'name3.json')
    json3_path = Path(json3_path_str)
    json3_path.write_text('nonzero')

    pw = pwd.getpwnam("root")
    rt_uid = pw.pw_uid
    rt_gid = pw.pw_gid
    user = getenv('USER', 'nobody')
    pw = pwd.getpwnam(user)
    pi_uid = pw.pw_uid
    pi_gid = pw.pw_gid

    mock_resp = mock.Mock()
    mock_resp.raise_status = 200
    mock_resp.json = mock.Mock(
        return_value=[
            {'name': 'name1', 'data': {'key1': 'v1'}, 'linux_user': user},
            {'name': 'name2', 'data': {'key1': 'v21', 'key2': 'v22'}, 'linux_user': user},
            {'name': 'name2', 'data': {'key3': 'v23'}, 'linux_user': ''},
        ]
    )
    mock_resp.return_value.ok = True
    with mock.patch('agent.logger'), \
            mock.patch('agent.can_read_cert') as cr, \
            mock.patch('requests.request') as req, \
            mock.patch('os.chmod') as chm, \
            mock.patch('os.chown') as chw:

        cr.return_value = True
        req.return_value = mock_resp
        mock_resp.return_value.ok = True
        agent.fetch_credentials(False)

        assert Path.exists(tmpdir / user / 'name1.json')
        assert Path.exists(tmpdir / user / 'name2.json')
        assert Path.exists(tmpdir / 'name2.json')
        assert Path.exists(json3_path) is False

        pi_dir_path = str(tmpdir / user)
        pi_name1_path = str(tmpdir / user / 'name1.json')
        pi_name2_path = str(tmpdir / user / 'name2.json')
        rt_name2_path = str(tmpdir / 'name2.json')
        with open(pi_name1_path) as f:
            assert json.load(f) == {"key1": "v1"}
        with open(pi_name2_path) as f:
            assert json.load(f) == {"key1": "v21", "key2": "v22"}
        with open(rt_name2_path) as f:
            assert json.load(f) == {"key3": "v23"}

        chm.assert_has_calls([
            mock.call(pi_name1_path, 0o400),
            mock.call(pi_name2_path, 0o400),
            mock.call(rt_name2_path, 0o400),
        ], any_order=True)

        chw.assert_has_calls([
            mock.call(rt_name2_path, rt_uid, rt_gid),
            mock.call(pi_dir_path, pi_uid, pi_gid),
            mock.call(pi_name2_path, pi_uid, pi_gid),
            mock.call(pi_name1_path, pi_uid, pi_gid)
        ], any_order=True)


def test_fetch_credentials_no_dir(tmpdir):
    executor.Locker.LOCKDIR = str(tmpdir)
    agent.CREDENTIALS_PATH = str(tmpdir / 'notexist')
    file_path1 = tmpdir / 'notexist' / 'name1.json'
    file_path2 = tmpdir / 'notexist' / 'name2.json'

    mock_resp = mock.Mock()
    mock_resp.raise_status = 200
    mock_resp.json = mock.Mock(
        return_value=[
            {'name': 'name1', 'data': {'key1': 'v1'}},
            {'name': 'name2', 'data': {'key1': 'v21'}}
        ]
    )
    mock_resp.return_value.ok = True
    with mock.patch('agent.logger'), \
            mock.patch('agent.can_read_cert') as cr, \
            mock.patch('requests.request') as req:

        cr.return_value = True
        req.return_value = mock_resp
        mock_resp.return_value.ok = True
        agent.fetch_credentials(False)

        assert Path.exists(file_path1)
        assert Path.exists(file_path2)
        with open(str(file_path1)) as f:
            assert json.load(f) == {"key1": "v1"}

        with open(str(file_path2)) as f:
            assert json.load(f) == {"key1": "v21"}


def test_fetch_device_metadata(tmpdir):
    executor.Locker.LOCKDIR = str(tmpdir)
    json3_path_str = str(tmpdir / 'name3.json')
    json3_path = Path(json3_path_str)
    json3_path.write_text('nonzero')
    agent.SECRET_DEV_METADATA_PATH = str(json3_path_str)

    mock_resp = mock.Mock()
    mock_resp.raise_status = 200
    mock_resp.json = mock.Mock(
        return_value={
            'manufacturer': 'Raspberry Pi',
            'device_id': '7fe5ef257a7a4ee38841a5f8bf672791.d.wott-dev.local',
            'string': 'test string value',
            'array': [1, 2, 3, 4, 5, 'penelopa'],
            'test': 'value',
            'model': 'Pi 3 Model B+'
        }
    )
    mock_resp.return_value.ok = True
    with mock.patch('agent.can_read_cert') as cr, \
            mock.patch('requests.request') as req, \
            mock.patch('agent.logger'), \
            mock.patch('os.chmod') as chm:

        cr.return_value = True
        req.return_value = mock_resp
        mock_resp.return_value.ok = True
        agent.fetch_device_metadata(False, agent.logger)

        assert Path.exists(json3_path)

        with open(json3_path_str) as f:
            assert json.load(f) == {
                'manufacturer': 'Raspberry Pi',
                'device_id': '7fe5ef257a7a4ee38841a5f8bf672791.d.wott-dev.local',
                'string': 'test string value',
                'array': [1, 2, 3, 4, 5, 'penelopa'],
                'test': 'value',
                'model': 'Pi 3 Model B+'
            }

        chm.assert_has_calls([
            mock.call(json3_path_str, 0o600),
        ])


def test_enroll_device_ok(tmpdir):
    executor.Locker.LOCKDIR = str(tmpdir)
    message = "Node d3d301961e6c4095b59583083bdec290.d.wott-dev.local enrolled successfully."

    mock_resp = mock.Mock()

    with mock.patch('requests.post') as req, \
            mock.patch('agent.logger') as prn:
        req.return_value = mock_resp
        req.return_value.ok = True
        req.return_value.status_code = 200
        req.return_value.content = {}
        assert agent.enroll_device(
            enroll_token="1dc99d48e67b427a9dc00b0f19003802",
            device_id="d3d301961e6c4095b59583083bdec290.d.wott-dev.local",
            claim_token="762f9d82-4e10-4d8b-826c-ac802219ec47"
        )
        assert prn.error.call_count == 0
        assert prn.info.call_count == 1
        assert mock.call(message) in prn.info.mock_calls


def test_enroll_device_nok(tmpdir):
    executor.Locker.LOCKDIR = str(tmpdir)
    error_content = {
        "key": ["Pairnig-token not found"],
        "claim_token": ["Claim-token not found"]
    }

    mock_resp = mock.Mock()
    mock_resp.json = mock.Mock(return_value=error_content)

    with mock.patch('requests.post') as req, \
            mock.patch('agent.logger') as prn:

        req.return_value = mock_resp
        req.return_value.ok = False
        req.return_value.status_code = 400
        req.return_value.reason = "Bad Request"
        req.return_value.content = error_content
        assert not agent.enroll_device(
            enroll_token="1dc99d48e67b427a9dc00b0f19003802",
            device_id="d3d301961e6c4095b59583083bdec290.d.wott-dev.local",
            claim_token="762f9d82-4e10-4d8b-826c-ac802219ec47"
        )
        assert prn.error.call_count == 4
        assert mock.call('Failed to enroll node...') in prn.error.mock_calls
        assert mock.call('Code:400, Reason:Bad Request') in prn.error.mock_calls
        assert mock.call('claim_token : Claim-token not found') in prn.error.mock_calls
        assert mock.call('key : Pairnig-token not found') in prn.error.mock_calls
        assert prn.debug.call_count == 2
        assert mock.call("enroll-device :: [RECEIVED] Enroll by token post: 400") in prn.debug.mock_calls
        log_dbg_text = "enroll-device :: [RECEIVED] Enroll by token post: {}".format(error_content)
        assert mock.call(log_dbg_text) in prn.debug.mock_calls


def _mock_repr(self):
    return self.return_value


def test_enroll_in_operation_mode_ok(tmpdir):
    executor.Locker.LOCKDIR = str(tmpdir)
    agent.INI_PATH = str(tmpdir / 'config.ini')
    with open(agent.INI_PATH, "w") as f:
        f.write("[DEFAULT]\nenroll_token = 123456\nrollback_token = 123456\n")

    mock_resp = mock.Mock()
    mock_resp.json = mock.Mock(return_value={})

    mock_mtls = mock.Mock()
    mock_mtls.json = mock.Mock(return_value={'claim_token': '3456', 'claimed': False})
    mock_mtls.return_value = "TestClaimToken"

    with mock.patch('agent.mtls_request') as mtls, \
            mock.patch('requests.post') as req, \
            mock.patch('agent.logger') as prn:
        req.return_value = mock_resp
        req.return_value.ok = True
        req.return_value.status_code = 200
        req.return_value.content = {}

        mtls.return_value = mock_mtls
        mtls.return_value.__repr__ = _mock_repr
        mtls.return_value.ok = True
        mtls.return_value.status_code = 200
        mtls.return_value.content = {}
        agent.try_enroll_in_operation_mode('deviceid000', True)

        assert mock.call.info('Enroll token found. Trying to automatically enroll the node.') in prn.method_calls
        assert mock.call.debug('\nDASH_ENDPOINT: %s\nWOTT_ENDPOINT: %s\nMTLS_ENDPOINT: %s', 'http://localhost:8000',
                               'http://localhost:8001/api', 'http://localhost:8002/api') in prn.method_calls
        assert mock.call.debug('[RECEIVED] Get Node Claim Info: TestClaimToken') in prn.method_calls
        assert mock.call.info('Node deviceid000 enrolled successfully.') in prn.method_calls
        assert mock.call.info('Update config...') in prn.method_calls
        assert len(prn.method_calls) == 5
        with open(agent.INI_PATH) as f:
            assert f.read() == "[DEFAULT]\nrollback_token = 123456\n\n"


def test_enroll_in_operation_mode_enroll_fail(tmpdir):
    error_content = {
        "key": ["Pairnig-token not found"],
    }
    executor.Locker.LOCKDIR = str(tmpdir)
    file_content = "[DEFAULT]\nenroll_token = 123456\nrollback_token = 123456\n"
    agent.INI_PATH = str(tmpdir / 'config.ini')
    with open(agent.INI_PATH, "w") as f:
        f.write(file_content)

    mock_resp = mock.Mock()
    mock_resp.json = mock.Mock(return_value=error_content)

    mock_mtls = mock.Mock()
    mock_mtls.json = mock.Mock(return_value={'claim_token': '3456', 'claimed': False})
    mock_mtls.return_value = "TestClaimToken"

    with mock.patch('agent.mtls_request') as mtls, \
            mock.patch('requests.post') as req, \
            mock.patch('agent.logger') as prn:
        req.return_value = mock_resp
        req.return_value.ok = False
        req.return_value.status_code = 400
        req.return_value.content = {}
        req.return_value.reason = "Bad Request"
        req.return_value.content = error_content

        mtls.return_value = mock_mtls
        mtls.return_value.__repr__ = _mock_repr
        mtls.return_value.ok = True
        mtls.return_value.status_code = 200
        mtls.return_value.content = {}
        agent.try_enroll_in_operation_mode('deviceid000', True)

        assert mock.call.info('Enroll token found. Trying to automatically enroll the node.') in prn.method_calls
        assert mock.call.debug('\nDASH_ENDPOINT: %s\nWOTT_ENDPOINT: %s\nMTLS_ENDPOINT: %s', 'http://localhost:8000',
                               'http://localhost:8001/api', 'http://localhost:8002/api') in prn.method_calls
        assert mock.call.debug('[RECEIVED] Get Node Claim Info: TestClaimToken') in prn.method_calls
        assert mock.call.error('Failed to enroll node...') in prn.method_calls
        assert mock.call.error('Code:400, Reason:Bad Request') in prn.method_calls
        assert mock.call.error('key : Pairnig-token not found') in prn.method_calls
        assert mock.call.debug('enroll-device :: [RECEIVED] Enroll by token post: 400') in prn.method_calls
        assert mock.call.debug("enroll-device :: [RECEIVED] Enroll by token post: {"
                               "'key': ['Pairnig-token not found']}") in prn.method_calls
        assert mock.call.error('Node enrolling failed. Will try next time.') in prn.method_calls
        assert len(prn.method_calls) == 9
        with open(agent.INI_PATH) as f:
            assert f.read() == file_content


def test_enroll_in_operation_mode_already_claimed(tmpdir):
    executor.Locker.LOCKDIR = str(tmpdir)
    agent.INI_PATH = str(tmpdir / 'config.ini')
    with open(agent.INI_PATH, "w") as f:
        f.write("[DEFAULT]\nenroll_token = 123456\nrollback_token = 123456\n")

    mock_mtls = mock.Mock()
    mock_mtls.json = mock.Mock(return_value={'claim_token': '3456', 'claimed': True})
    mock_mtls.return_value = "TestClaimToken"

    with mock.patch('agent.mtls_request') as mtls, \
            mock.patch('agent.logger') as prn:
        mtls.return_value = mock_mtls
        mtls.return_value.__repr__ = _mock_repr
        mtls.return_value.ok = True
        mtls.return_value.status_code = 200
        mtls.return_value.content = {}
        agent.try_enroll_in_operation_mode('deviceid000', True)

        assert mock.call.info('Enroll token found. Trying to automatically enroll the node.') in prn.method_calls
        assert mock.call.debug('\nDASH_ENDPOINT: %s\nWOTT_ENDPOINT: %s\nMTLS_ENDPOINT: %s', 'http://localhost:8000',
                               'http://localhost:8001/api', 'http://localhost:8002/api') in prn.method_calls
        assert mock.call.debug('[RECEIVED] Get Node Claim Info: TestClaimToken') in prn.method_calls
        assert mock.call.info('The node is already claimed. No enrolling required.') in prn.method_calls
        assert mock.call.info('Update config...') in prn.method_calls
        assert len(prn.method_calls) == 5
        with open(agent.INI_PATH) as f:
            assert f.read() == "[DEFAULT]\nrollback_token = 123456\n\n"


def test_enroll_in_operation_mode_no_claim_info(tmpdir):   # or server error
    executor.Locker.LOCKDIR = str(tmpdir)
    agent.INI_PATH = str(tmpdir / 'config.ini')
    file_content = "[DEFAULT]\nenroll_token = 123456\nrollback_token = 123456\n"
    with open(agent.INI_PATH, "w") as f:
        f.write(file_content)
    mock_mtls = mock.Mock()
    mock_mtls.json = mock.Mock(return_value={})

    with mock.patch('agent.mtls_request') as mtls, \
            mock.patch('agent.logger') as prn:
        mtls.return_value = mock_mtls
        mtls.return_value.__repr__ = _mock_repr
        mtls.return_value.ok = False
        mtls.return_value.status_code = 400
        mtls.return_value.content = {}
        agent.try_enroll_in_operation_mode('deviceid000', True)

        assert mock.call.info('Enroll token found. Trying to automatically enroll the node.') in prn.method_calls
        assert mock.call.debug('\nDASH_ENDPOINT: %s\nWOTT_ENDPOINT: %s\nMTLS_ENDPOINT: %s', 'http://localhost:8000',
                               'http://localhost:8001/api', 'http://localhost:8002/api') in prn.method_calls
        assert mock.call.error('Did not manage to get claim info from the server.') in prn.method_calls
        assert len(prn.method_calls) == 3
        with open(agent.INI_PATH) as f:
            assert f.read() == file_content


def test_enroll_in_operation_mode_no_token(tmpdir):   # or server error
    executor.Locker.LOCKDIR = str(tmpdir)
    file_content = "[DEFAULT]\nrollback_token = 123456\n"
    agent.INI_PATH = str(tmpdir / 'config.ini')
    with open(agent.INI_PATH, "w") as f:
        f.write(file_content)

    with mock.patch('agent.logger') as prn:
        agent.try_enroll_in_operation_mode('deviceid000', True)
        assert len(prn.method_calls) == 0
        with open(agent.INI_PATH) as f:
            assert f.read() == file_content


@pytest.mark.vcr
def test_deb_package_cache(tmpdir, cert, key, raspberry_cpuinfo, net_connections_fixture, uptime):
    """
    Test the package list cahing behavior.
    """
    crt_path = tmpdir / 'client.crt'
    key_path = tmpdir / 'client.key'
    agent.CERT_PATH = str(tmpdir)
    agent.CLIENT_CERT_PATH = str(crt_path)
    agent.CLIENT_KEY_PATH = str(key_path)
    Path(agent.CLIENT_CERT_PATH).write_text(cert)
    Path(agent.CLIENT_KEY_PATH).write_text(key)

    with mock.patch(
            'builtins.open',
            mock.mock_open(read_data=raspberry_cpuinfo),
            create=True
    ), \
            mock.patch('socket.getfqdn') as getfqdn, \
            mock.patch('psutil.net_connections') as net_connections, \
            mock.patch('agent.iptables_helper.dump') as fr, \
            mock.patch('agent.security_helper.check_for_default_passwords') as chdf, \
            mock.patch('agent.security_helper.process_scan') as ps, \
            mock.patch('agent.iptables_helper.block_ports') as bp, \
            mock.patch('agent.iptables_helper.block_networks') as bn, \
            mock.patch('agent.journal_helper.logins_last_hour') as logins, \
            mock.patch('apt.Cache') as aptCache, \
            mock.patch('agent.mtls_request', wraps=agent.mtls_request) as mtls, \
            mock.patch(
                'builtins.open',
                mock.mock_open(read_data=uptime),
                create=True
            ):  # noqa E213
        deb_pkg = mock.MagicMock()
        deb_pkg.installed.package.name = 'thepackage'
        deb_pkg.installed.source_name = 'thepackage'
        deb_pkg.installed.version = 'theversion'
        deb_pkg.installed.source_version = 'theversion'
        deb_pkg.installed.architecture = 'i386'
        aptCache.return_value = [deb_pkg]
        net_connections.return_value = net_connections_fixture[0],
        fr.return_value = {}
        chdf.return_value = False
        ps.return_value = []
        getfqdn.return_value = 'localhost'
        bp.return_value = None
        bn.return_value = None
        logins.return_value = {}

        # If the server doesn't have our package list yet it won't send deb_package_hash.
        # In this case send_ping should send the package list and the hash.
        agent.MTLS_ENDPOINT = 'https://mtls.wott.io'
        agent.send_ping()
        deb_packages_json = mtls.call_args[1]['json']['deb_packages']
        assert deb_packages_json['hash'] == 'e88b4875f08ede2e1068e117bdaa80ac'

        # The second time the server already knows the hash and sends it in deb_package_hash.
        # send_ping should not send deb_packages in this case.
        agent.MTLS_ENDPOINT = 'https://mtls.wott.io'
        agent.send_ping()
        deb_packages_json = mtls.call_args[1]['json']
        assert 'deb_packages' not in deb_packages_json


def _is_parallel(tmpdir, use_lock: bool, use_pairs: bool = False):
    """
    Execute two "sleepers" at once.
    :param tmpdir: temp directory where logs and locks will be stored (provided by pytest)
    :param use_lock: use executor.Locker to execute exclusively
    :return: whether the two tasks were seen executing in parallel (boolean value)
    """
    def _work(f: Path):
        """The actual workload: sleep and write before/after timestamps to provided file"""
        of = f.open('a+')
        of.write('{} '.format(time.time()))
        time.sleep(0.1)
        of.write('{}\n'.format(time.time()))

    def sleeper(lock: bool, f: Path, lockname: str):
        """This task will be executed by executor."""
        executor.Locker.LOCKDIR = str(tmpdir)  # can't use /var/lock in CircleCI environment
        if lock:
            with executor.Locker(lockname):
                _work(f)
        else:
            _work(f)

    def stop_exe():
        """Stop execution of tasks launched by executor."""
        for fut in futs:
            fut.cancel()
        for exe in exes:
            exe.stop()
        asyncio.get_event_loop().stop()

    def find_parallel(first_pairs, second_pairs):
        parallel = False
        for begin1, end1 in first_pairs:
            # Find a pair in second_pairs overlapping with first_pair.
            # That means execution was overlapped (parallel).
            for begin2, end2 in second_pairs:
                if begin2 <= begin1 <= end2 or begin2 <= end1 <= end2:
                    parallel = True
                    break
            if parallel:
                break
        return parallel

    def is_parallel(timestamp_files):
        # Parse timestamp files. Split them into (begin, end) tuples.
        file_time_pairs = []
        for f in timestamp_files:
            of = f.open('r')
            times = []
            for line in of.read().splitlines():
                begin, end = line.split()
                times.append((float(begin), float(end)))
            file_time_pairs.append(times)

        first_pairs, second_pairs = file_time_pairs
        return find_parallel(first_pairs, second_pairs) or find_parallel(second_pairs, first_pairs)

    # Schedule two identical tasks to executor. They will write before/after timestamps
    # to their files every 100 ms.
    test_files = [tmpdir / 'test_locker_' + str(i) for i in range(2)]
    exes = [executor.Executor(0.5, sleeper, (use_lock, test_file, 'one')) for test_file in test_files]

    # If testing independent locking, schedule another couple of tasks with another lock and another
    # set of timestamp files.
    if use_pairs:
        test_files_2 = [tmpdir / 'test_locker_2_' + str(i) for i in range(2)]
        exes += [executor.Executor(0.5, sleeper, (use_lock, test_file, 'two')) for test_file in test_files_2]

    futs = [executor.schedule(exe) for exe in exes]

    # Stop this after 3 seconds
    asyncio.get_event_loop().call_later(3, stop_exe)
    executor.spin()
    if use_lock:
        # When using Locker the tasks need some additional time to stop.
        time.sleep(3)

    if use_pairs:
        # If testing independent locking, find out:
        # - whether first couple of tasks were executed in parallel
        # - whether second couple of tasks were executed in parallel
        # - whether tasks from both couples were executed in parallel
        return is_parallel(test_files), \
            is_parallel(test_files_2), \
            is_parallel((test_files[0], test_files_2[0]))
    else:
        return is_parallel(test_files)


def test_locker(tmpdir):
    assert not _is_parallel(tmpdir, True)


def test_no_locker(tmpdir):
    assert _is_parallel(tmpdir, False)


def test_independent_lockers(tmpdir):
    one, two, both = _is_parallel(tmpdir, True, True)
    assert (one, two, both) == (False, False, True)


def test_selinux_status():
    with mock.patch('selinux.is_selinux_enabled') as selinux_enabled,\
            mock.patch('selinux.security_getenforce') as getenforce:

        selinux_enabled.return_value = 1
        getenforce.return_value = 1
        assert selinux_status() == {'enabled': True, 'mode': 'enforcing'}

        selinux_enabled.return_value = 1
        getenforce.return_value = 0
        assert selinux_status() == {'enabled': True, 'mode': 'permissive'}

        selinux_enabled.return_value = 0
        assert selinux_status() == {'enabled': False, 'mode': None}


def test_kernel_cmdline(cmdline):
    class mockPath():
        def __init__(self, filename):
            self._filename = filename

        def read_text(self):
            return cmdline

    with mock.patch('agent.os_helper.Path', mockPath):
        cmdline = kernel_cmdline()
        assert cmdline['one'] == ''
