from .proxy import *
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
import MySQLdb
import socket
import threading
import json


class Singleton:
    __instance = None

    @classmethod
    def __getInstance(cls):
        return cls.__instance

    @classmethod
    def instance(cls, *args, **kargs):
        cls.__instance = cls(*args, **kargs)
        cls.instance = cls.__getInstance
        return cls.__instance


class Scheduler:

    def __init__(self, db, proxy_list):
        self.db = db
        self.static_proxy_list = proxy_list

    def check_slave_alive(self):
        node_proxy_list = ProxyManager.instance().node_proxy_list
        closed_list = list()

        for node_proxy in node_proxy_list.values():
            try:
                cmd_socket = socket.fromfd(node_proxy['fd'], socket.AF_INET, socket.SOCK_STREAM)
                cmd_socket.sendall(b"")
            except Exception as ex:
                closed_list.append(node_proxy['fd'])
                node_proxy = node_proxy['node_proxy']
                node_proxy.stop()
                print("에러 : {}".format(ex))

        for closed in closed_list:

            del node_proxy_list[closed]

    def update_data_amount(self):

        cur = self.db.cursor()

        commit_info = list()

        for static_proxy in self.static_proxy_list:
            in_data, out_data = static_proxy.get_data_amount()

            insert_info = (static_proxy.listen_port, in_data, out_data)
            commit_info.append(insert_info)

        query = """INSERT INTO DATA_USE_AMOUNT (static_port, in_data, out_data) 
                    SELECT A.static_port, A.in_data, A.out_data
                    FROM (SELECT %s AS static_port,
                                 %s AS in_data,
                                 %s AS out_data
                            FROM DUAL
                         )A
                    ON DUPLICATE KEY
                    UPDATE in_data = DATA_USE_AMOUNT.in_data + A.in_data,
                            out_data = DATA_USE_AMOUNT.out_data + A.out_data
                """
        cur.executemany(query, commit_info)
        self.db.commit()

        cur.close()

    def check_not_use_proxy(self):

        cur = self.db.cursor()

        query = "SELECT static_port, update_time FROM data_use_amount"
        cur.execute(query)
        results = cur.fetchall()

        current_time = datetime.now()

        for result in results:
            not_use_date = (current_time - result[1]).days
            if not_use_date > 3:
                ProxyManager.instance().del_static_proxy(result[0])

        query = "SELECT * FROM static_port_forwarding_info"
        cur.execute(query)
        results = cur.fetchall()

        current_date = current_time.date()

        for result in results:
            limit = (current_date - result[4]).days
            if limit >= 0:
                ProxyManager.instance().del_static_proxy(result[0])

        cur.close()

    def start(self):
        print("Scheduler start")

        scheduler = BackgroundScheduler()
        scheduler.start()

        scheduler.add_job(self.check_slave_alive, 'interval', seconds=30, id='slave_checker')
        scheduler.add_job(self.update_data_amount, 'interval', seconds=30, id='data_checker')
        scheduler.add_job(self.check_not_use_proxy, 'interval', days=1, start_date=datetime.now(), id='proxy_remover')


class ProxyManager(Singleton):

    def __init__(self):

        print("ProxyManager On")

        self.default = 20000
        #가변 포트
        self.proxy_list = list()
        self.proxy_list.append(DynamicProxy(1, 30001))
        self.proxy_list.append(DynamicProxy(2, 30002))
        self.proxy_list.append(DynamicProxy(3, 30003))
        self.proxy_list.append(DynamicProxy(4, 30004))
        self.proxy_list.append(DynamicProxy(5, 30005))

        # 고정 포트
        #proxy info in database
        self.static_proxy_info = None
        #listening proxy list
        self.static_proxy_list = list()
        #fd , socket
        self.node_proxy_list = dict()

        self.db = MySQLdb.connect(host="127.0.0.1", user="root", passwd="hamonsoft", db="pilot")
        self.set_proxy_info()

        self.scheduler = Scheduler(self.db, self.static_proxy_list)
        self.scheduler.start()

        self.listen_slave()

    def close_command(self, transfer_info):

        target_proxy = self.node_proxy_list[int(transfer_info['node_proxy_key'])]
        node = target_proxy['node_proxy']

        fd = target_proxy['fd']
        cmd_socket = socket.fromfd(fd, socket.AF_INET, socket.SOCK_STREAM)

        info = {'command': 'close'}
        cmd_socket.sendall(json.dumps(info).encode())

        node.stop()
        del self.node_proxy_list[int(transfer_info['node_proxy_key'])]
        print(self.node_proxy_list)

    def transfer_command(self, transfer_info):

        target_proxy = self.node_proxy_list[int(transfer_info['node_proxy_key'])]
        target_proxy['node_proxy'].set_des(transfer_info['des_ip'], transfer_info['des_port'])

    def listen_slave(self):
        threading.Thread(target=self.slave_accept).start()

    def slave_accept(self):

        slave_listen_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        slave_listen_socket.bind(('localhost', 8001))
        slave_listen_socket.listen()

        while True:
            slave_data_socket, slave_info = slave_listen_socket.accept()

            print("hi")
            info = {'fd': slave_data_socket.fileno()}
            slave_data_socket.sendall(json.dumps(info).encode())

            data = slave_data_socket.recv(2048)

            data = json.loads(data.decode('utf-8'))

            if data['message'] == 'ok':
                print(slave_data_socket.fileno)

                self.default = self.default + 1

                self.slave_proxy = NodeProxy(slave_data_socket, self.default)
                self.node_proxy_list[slave_data_socket.fileno()] = {'node_proxy': self.slave_proxy,
                                                                    'fd': slave_data_socket.fileno(),
                                                                    'connect_time': datetime.now(),
                                                                    'ip_address': slave_info,
                                                                    'listening_port': self.default}

                self.slave_proxy.listen_start()
                self.slave_proxy = None

                print(self.node_proxy_list)

            if data['message'] == 'set':
                target = self.node_proxy_list[data['fd']]['node_proxy']
                print(slave_data_socket)
                connection_thread = threading.Thread(target=target.connection, args=(target.src_socket, slave_data_socket))
                connection_thread.daemon = True
                connection_thread.start()

            print('no')

    def set_proxy_info(self):

        cur = self.db.cursor()

        #get proxy_info from database
        query = "SELECT * FROM static_port_forwarding_info"
        cur.execute(query)
        self.static_proxy_info = cur.fetchall()
        print("Proxy information : {}".format(self.static_proxy_info))

        for proxy_info in self.static_proxy_info:

            proxy = StaticProxy(proxy_info[0])
            proxy.set_des(proxy_info[1], proxy_info[2])
            self.static_proxy_list.append(proxy)
            proxy.listen_start()

        cur.close()

    def listen_status(self):
        status = list()
        for proxy in self.proxy_list:
            info = {'port_number': proxy.listen_port,
                    'listening_state': proxy.listen_flag,
                    'pick_state': proxy.timer_use_yn}

            status.append(info)
        return status

    def add_static_proxy(self, static_port, des_ip, des_port):

        proxy = StaticProxy(static_port)
        proxy.set_des(des_ip, des_port)
        self.static_proxy_list.append(proxy)
        proxy.listen_start()

    def del_static_proxy(self, static_port):

        for proxy in self.static_proxy_list:
            if proxy.listen_port == int(static_port):
                proxy.stop()
                self.static_proxy_list.remove(proxy)
                break

    def get_proxy(self, number):

        return self.proxy_list.__getitem__(number - 1)

