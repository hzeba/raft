import sys
import json
import gevent
import random

from gevent import socket
from gevent import event


rng = random.Random()
rng.seed(0)


def election_timer():
    timeout = 5 * rng.randint(50, 100) / 100

    timer = gevent.Timeout(timeout)
    timer.start()

    return timer


class raft(object):
    def __init__(self, nodes, node_id):
        self.current_term = 0
        self.voted_for = None
        self.log = []

        self.commit_index = 0
        self.last_applied = 0

        self.nodes = nodes
        self.node_id = node_id
        self.quorum = int(len(nodes) / 2 + 1)

        self.demote = event.Event()
        self.reset_timer = event.Event()

    def request_vote(self, term, candidate_id, last_log_index, last_log_term):
        demote = term > self.current_term

        if demote == True:
            print('request_vote: new leader found, demoting...')

            self.current_term = term
            self.voted_for = None

            self.demote.set()

        if term < self.current_term:
            print('request_vote: vote to #%d denied' % candidate_id)
            return {'term': self.current_term, 'vote_granted': False}

        if self.voted_for != None and self.voted_for != candidate_id:
            print('request_vote: vote to #%d denied' % candidate_id)
            return {'term': self.current_term, 'vote_granted': False}

        if len(self.log) != 0:
            if last_log_term < self.log[-1][0]:
                print('request_vote: vote to #%d denied' % candidate_id)
                return {'term': self.current_term, 'vote_granted': False}

            if last_log_term == self.log[-1][0] and last_log_index < len(self.log):
                print('request_vote: vote to #%d denied' % candidate_id)
                return {'term': self.current_term, 'vote_granted': False}

        self.voted_for = candidate_id

        print('request_vote: vote to #%d granted' % candidate_id)
        return {'term': self.current_term, 'vote_granted': True}

    def append_entries(self, term, leader_id, prev_log_index, prev_log_term, entries, leader_commit_index):
        demote = term > self.current_term

        if demote == True:
            print('append_entries: new leader found, demoting...')

            self.current_term = term
            self.voted_for = None

            self.demote.set()

        if term < self.current_term:
            return {'term': self.current_term, 'success': False}

        self.reset_timer.set()

        if prev_log_index >= len(self.log):
            return {'term': self.current_term, 'success': False}

        # process log

        return {'term': self.current_term, 'success': True}


class follower(object):
    def __init__(self, raft):
        self.raft = raft
        self.raft.demote.clear()

    def run(self):
        while True:
            timer = election_timer()

            try:
                self.raft.reset_timer.wait()
                self.raft.reset_timer.clear()

            except gevent.timeout.Timeout:
                break

            finally:
                timer.close()


class candidate(object):
    def __init__(self, raft):
        self.raft = raft
        self.raft.demote.clear()

        self.votes_granted = 0
        self.votes_denied = 0

        self.vote_processed = event.Event()

    def _send_vote_request(self, data, node_id, node_port):
        print('sending request_vote to #%d' % node_id)

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        s.sendto(data, 0, ('127.0.0.1', node_port))

        data, addr = s.recvfrom(1024)
        data = json.loads(data)

        term = data['term']
        vote_granted = data['vote_granted']

        if term > self.raft.current_term:
            assert(vote_granted == False)

            print('new leader found #%d, switching to follower...' % node_id)

            self.raft.current_term = term
            self.raft.demote.set()

            return

        if vote_granted == True:
            print('vote granted from #%d' % node_id)
            self.votes_granted += 1

        else:
            print('vote denied from #%d' % node_id)
            self.votes_denied += 1

        self.vote_processed.set()

    def _send_vote_requests(self):
        last_log_index = -1
        last_log_term = -1

        if len(self.raft.log) != 0:
            last_log_index = len(self.raft.log) - 1
            last_log_term = self.raft.log[last_log_index][0]

        data = {
            'function': 'request_vote',
            'term': self.raft.current_term,
            'candidate_id': self.raft.node_id,
            'last_log_index': last_log_index,
            'last_log_term': last_log_term
        }

        data = bytes(json.dumps(data), 'utf-8')

        jobs = []

        for node_id, node_port in self.raft.nodes.items():
            if node_id == self.raft.node_id:
                continue

            jobs.append(gevent.spawn(self._send_vote_request, data, node_id, node_port))

        return jobs

    def run(self):
        while self.raft.demote.is_set() == False and \
              self.votes_granted < self.raft.quorum:

            self.raft.current_term += 1
            self.raft.voted_for = self.raft.node_id

            print('starting new term #%d' % self.raft.current_term)

            self.votes_granted = 1

            timer = election_timer()
            jobs = None

            try:
                jobs = self._send_vote_requests()

                while self.raft.demote.is_set() == False and \
                      self.votes_granted < self.raft.quorum:

                    gevent.wait([self.vote_processed, self.raft.demote], count=1)
                    self.vote_processed.clear()

            except gevent.timeout.Timeout:
                continue

            finally:
                timer.close()
                gevent.killall(jobs)

            return self.raft.demote.is_set() == False


class leader(object):
    def __init__(self, raft):
        self.raft = raft
        self.raft.demote.clear()

        self.next_index = []
        self.match_index = []

    def _send_heartbeat(self, data, node_id, node_port):
        print('sending append_entries to #%d' % node_id)

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        s.sendto(data, 0, ('127.0.0.1', node_port))

        data, addr = s.recvfrom(1024)
        data = json.loads(data)

        term = data['term']
        success = data['success']

        if term > self.raft.current_term:
            print('discovered higher term %d from #%d, switching to follower...' % (term, node_id))

            self.raft.current_term = term
            self.raft.demote.set()

    def _send_heartbeats(self):
        prev_log_index = -1
        prev_log_term = -1

        if len(self.raft.log) != 0:
            prev_log_index = len(self.raft.log) - 1
            prev_log_term = self.raft.log[prev_log_index][0]

        data = {
            'function': 'append_entries',
            'term': self.raft.current_term,
            'leader_id': self.raft.node_id,
            'prev_log_index': prev_log_index,
            'prev_log_term': prev_log_term,
            'entries': [],
            'leader_commit_index': self.raft.commit_index
        }

        data = bytes(json.dumps(data), 'utf-8')

        jobs = []

        for node_id, node_port in self.raft.nodes.items():
            if node_id == self.raft.node_id:
                continue

            jobs.append(gevent.spawn(self._send_heartbeat, data, node_id, node_port))

        return jobs

    def run(self):
        while self.raft.demote.is_set() == False:
            jobs = self._send_heartbeats()
            gevent.sleep(1.25)

            gevent.killall(jobs)


def load_nodes(conf):
    nodes = {}

    conf = json.loads(open(conf).read())

    for node_id, node_port in conf['nodes'].items():
        nodes[int(node_id)] = int(node_port)

    return nodes


def recv_thread(raft):
    conn = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    conn.bind(('127.0.0.1', nodes[node_id]))

    while True:
        data, addr = conn.recvfrom(1024)
        data = json.loads(data)

        if 'function' not in data:
            print('invalid request from %s: %s' % (addr, data))

        else:
            print(data, addr)

            if data['function'] == 'request_vote':
                data = raft.request_vote(data['term'],
                                         data['candidate_id'],
                                         data['last_log_index'],
                                         data['last_log_term'])

            elif data['function'] == 'append_entries':
                data = raft.append_entries(data['term'],
                                           data['leader_id'],
                                           data['prev_log_index'],
                                           data['prev_log_term'],
                                           data['entries'],
                                           data['leader_commit_index'])

            else:
                raise RuntimeError('invalid function from %s: %s' % (addr, data))

            data = bytes(json.dumps(data), 'utf-8')
            conn.sendto(data, 0, addr)


nodes = load_nodes(sys.argv[1])
node_id = int(sys.argv[2])

r = raft(nodes, node_id)
gevent.spawn(recv_thread, r)


while True:
    follower(r).run()

    print('leader timeout, promoting to candidate...')

    if candidate(r).run() == True:
        print('election won, promoting to leader...')
        leader(r).run()

        print('demoting to follower...')

    else:
        print('election lost, demoting to follower...')