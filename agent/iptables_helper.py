from typing import List, Tuple
from itertools import product
import logging
import ctypes as ct

import iptc
from pkg_resources import parse_version
from sh import iptables

logger = logging.getLogger('agent.iptables_helper')

TABLE = 'filter'
DROP_CHAIN = 'WOTT_LOG_DROP'
OUTPUT_CHAIN = 'WOTT_OUTPUT'
INPUT_CHAIN = 'WOTT_INPUT'

# Fix a bug in python-iptables by monkey-patching a couple of methods.
# The bug is actually fixed in:
#  https://github.com/chruss2/python-iptables/commit/282c790738a111b1ddc27b43ecb0acfab8b09024
#  and the bugfix is gonna be released in the next (after 0.14.0) release of python-iptables.
iptables_version = iptables('--version').split(maxsplit=2)[1]
upgrade = parse_version(iptc.version.__version__) <= parse_version('0.14.0') \
    and parse_version(iptables_version) >= parse_version('1.8.0')
downgrade = parse_version(iptc.version.__version__) >= parse_version('0.15.0-dev') \
    and parse_version(iptables_version) < parse_version('1.8.0')
if upgrade or downgrade:
    def find_match(self, name):
        if isinstance(name, str):
            name = name.encode()
        name = self._check_extname(name)

        ext = self._get_loaded_ext(name)
        if ext is not None:
            return ext

        if downgrade:
            iptc.xtables.xtables._xtables_matches.value = ct.c_void_p(None).value
            if iptc.xtables.xtables._xtables_pending_matches:
                iptc.xtables.xtables._xtables_pending_matches.value = ct.c_void_p(None).value

        match = iptc.xtables.xtables._xtables_find_match(name, iptc.xtables.XTF_TRY_LOAD, None)
        if not match:
            self._try_register(name)
            match = iptc.xtables.xtables._xtables_find_match(name, iptc.xtables.XTF_DONT_LOAD, None)
            if not match:
                return match

        m = ct.cast(match, ct.POINTER(self._match_struct))
        self._loaded(m[0].name, m)
        return m

    def find_target(self, name):
        if isinstance(name, str):
            name = name.encode()
        name = self._check_extname(name)

        ext = self._get_loaded_ext(name)
        if ext is not None:
            return ext

        if downgrade:
            iptc.xtables.xtables._xtables_targets.value = ct.c_void_p(None).value
            if iptc.xtables.xtables._xtables_pending_targets:
                iptc.xtables.xtables._xtables_pending_targets.value = ct.c_void_p(None).value

        target = iptc.xtables.xtables._xtables_find_target(name, iptc.xtables.XTF_TRY_LOAD)
        if not target:
            self._try_register(name)
            target = iptc.xtables.xtables._xtables_find_target(name, iptc.xtables.XTF_DONT_LOAD)
            if not target:
                return target

        t = ct.cast(target, ct.POINTER(self._target_struct))
        self._loaded(t[0].name, t)
        return t

    iptc.xtables.xtables.find_match = iptc.xtables.set_nfproto(find_match)
    iptc.xtables.xtables.find_target = iptc.xtables.set_nfproto(find_target)
    from . import iptc_helper
else:
    from . import iptc_helper


def dump():
    """
    Get all rules for all chains in all tables for both IPv4 and IPv6.
    """
    tables = {'v6': {}, 'v4': {}}
    for table_name, ipv6 in product(('filter', 'nat', 'mangle'), (False, True)):
        table = iptc_helper.dump_table(table_name, ipv6=ipv6).items()
        chains = {}
        for chain_name, chain in table:
            policy = iptc_helper.get_policy(table_name, chain_name, ipv6=ipv6)
            rules = {'rules': [rule for rule in chain if chain_name != OUTPUT_CHAIN]}
            if policy:
                rules['policy'] = policy
            chains[chain_name] = rules
        tables['v6' if ipv6 else 'v4'][table_name] = chains
    return tables


def prepare():
    """
    Add INPUT_CHAIN and OUTPUT_CHAIN to TABLE if they don't exist.
    Otherwise clear (flush) them.
    """
    for ipv6 in (False, True):
        if not iptc_helper.has_chain(TABLE, DROP_CHAIN, ipv6=ipv6):
            iptc_helper.add_chain(TABLE, DROP_CHAIN, ipv6=ipv6)
            iptc_helper.batch_add_rules(TABLE, [
                {'target': {'LOG': {'log-prefix': 'DROP: ', 'log-level': '3'}}},
                {'target': 'DROP'}
            ], chain=DROP_CHAIN, ipv6=ipv6)

        if not iptc_helper.has_chain(TABLE, INPUT_CHAIN, ipv6=ipv6):
            iptc_helper.add_chain(TABLE, INPUT_CHAIN, ipv6=ipv6)
        else:
            iptc_helper.flush_chain(TABLE, INPUT_CHAIN, ipv6=ipv6)

        if not iptc_helper.has_chain(TABLE, OUTPUT_CHAIN, ipv6=ipv6):
            iptc_helper.add_chain(TABLE, OUTPUT_CHAIN, ipv6=ipv6)
        else:
            iptc_helper.flush_chain(TABLE, OUTPUT_CHAIN, ipv6=ipv6)

        # Check for the first rule in OUTPUT and in INPUT and add it if missing.
        # The first rule jumps to WOTT_INPUT or WOTT_OUTPUT where we decide what to do with it.
        # If we don't block the packet it returns back to INPUT or OUTPUT and gets handled by existing rules.
        # This way we don't interfere with the filtering which was already configured on the device.
        for target_chain, chain in ((INPUT_CHAIN, 'INPUT'), (OUTPUT_CHAIN, 'OUTPUT')):
            # -I $chain -j $target_chain
            jump_to_target = {'target': target_chain}
            first_rule = iptc_helper.get_rule(TABLE, chain, 1, ipv6=ipv6)
            if jump_to_target != first_rule:
                # Another rule may have been added on top, which means our rule may be somewhere else.
                iptc_helper.delete_rule(TABLE, chain, jump_to_target, ipv6=ipv6, raise_exc=False)
                iptc_helper.add_rule(TABLE, chain, jump_to_target, 1, ipv6=ipv6)


def add_block_rules():
    """
    Adds rules which permit localhost and related/established connections
    (ipv4 and ipv6) and drops the rest of input traffic.
    """
    for ipv6 in (False, True):
        # -I WOTT_INPUT -i lo -j ACCEPT
        # -I WOTT_INPUT -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
        # -A WOTT_INPUT -j DROP
        iptc_helper.batch_add_rules(TABLE, [
            (INPUT_CHAIN, {
                'in-interface': 'lo',
                'target': 'ACCEPT'
            }, 1),
            (INPUT_CHAIN, {
                'conntrack': {'ctstate': 'RELATED,ESTABLISHED'},
                'target': 'ACCEPT'
            }, 2),
            (INPUT_CHAIN, {
                'target': 'DROP'
            }, 0)
        ], ipv6=ipv6)


def add_rules(table, chain, rules):
    """
    Insert rules into the chain for both ipv4 and ipv6.

    :param table: table name
    :param chain: chain name
    :param rules: a list of rules in iptc.easy format
    :return: None
    """
    for ipv6 in (False, True):
        rules_ipv = [rule for rule, is_ipv6 in rules if ipv6 == is_ipv6]
        iptc_helper.batch_add_rules(table, rules_ipv, chain=chain, ipv6=ipv6)


def block_ports(allow: bool, ports_data: List[Tuple[str, str, int, bool]]):
    """
    Block or allow incoming TCP/UDP packets to the ports supplied in the list.

    :param allow: True if policy is "allow by default" (which means: block the supplied ports)
    :param ports_data: dict of protocols/ports to be blocked or allowed
    :return: None
    """

    def remove_unspecified(r):
        if r['dst'] in ['0.0.0.0', '::']:
            del(r['dst'])
        return r

    rules = [(remove_unspecified({
        'protocol': proto,
        proto: {'dport': str(port)},
        'dst': host,
        'target': DROP_CHAIN if allow else 'ACCEPT'
    }), ipv6)
        for host, proto, port, ipv6 in ports_data]
    add_rules(TABLE, INPUT_CHAIN, rules)


def block_networks(network_list: List[Tuple[str, bool]]):
    """
    Block outgoing packets to the networks supplied in the list.

    :param network_list: list of IPs in dot-notation or subnets (<IP>/<mask>)
    :return: None
    """

    rules = [({'dst': n,
               'target': DROP_CHAIN,
               }, ipv6)
             for n, ipv6 in network_list]
    add_rules(TABLE, OUTPUT_CHAIN, rules)


def block(blocklist):
    policy = blocklist.get('policy', 'allow')
    try:
        prepare()
        block_networks(blocklist.get('block_networks', []))
        if policy == 'allow':
            block_ports(True, blocklist.get('block_ports', []))
        elif policy == 'block':
            block_ports(False, blocklist.get('allow_ports', []))
            add_block_rules()
        else:
            logger.error('Error: unknown policy "{}"'.format(policy))
    except iptc.IPTCError as e:
        logger.error('Error while updating iptables: %s', str(e))
        logger.debug(exc_info=True)
        if 'insmod' in str(e):
            logger.error('Error: failed to update iptables, try rebooting')
