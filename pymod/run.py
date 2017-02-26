import avro.schema
import decimal
import os
import sys
import time

from argo_nagios_ams_publisher.publish import FilePublisher, MessagingPublisher
from argo_nagios_ams_publisher.purge import Purger
from collections import deque
from datetime import datetime
from messaging.error import MessageError
from messaging.message import Message
from messaging.queue.dqs import DQS
from multiprocessing import Process, Lock, Event

class ConsumerQueue(Process):
    def __init__(self, *args, **kwargs):
        Process.__init__(self)
        self.init_attrs(kwargs)

        self.nmsgs_consumed = 0
        self.sess_consumed = 0

        self.seenmsgs = set()
        self.dirq = DQS(path=self.directory)
        self.inmemq = deque()
        self.pubnumloop = 1 if self.bulk > self.rate \
                          else self.rate / self.bulk
        kwargs.update({'inmemq': self.inmemq, 'pubnumloop': self.pubnumloop,
                       'dirq': self.dirq, 'filepublisher': False})
        self.publisher = self.publisher(*args, **kwargs)
        self.purger = Purger(*args, **kwargs)

    def init_attrs(self, confopts):
        for k in confopts.iterkeys():
            code = "self.{0} = confopts.get('{0}')".format(k)
            exec code

    def cleanup(self):
        self.unlock_dirq_msgs(self.seenmsgs)

    def stats(self, reset=False):
        def statmsg(hours):
            self.log.info('{0} {1}: consumed {2} msgs in {3:0.2f} hours'.format(self.__class__.__name__,
                                                                        self.name,
                                                                        self.nmsgs_consumed,
                                                                        hours))
        if reset:
            statmsg(self.statseveryhour)
            self.nmsgs_consumed = 0
            self.prevstattime = int(datetime.now().strftime('%s'))
        else:
            sincelaststat = time.time() - self.prevstattime
            statmsg(sincelaststat/3600)

    def run(self):
        self.prevstattime = int(datetime.now().strftime('%s'))
        termev = self.ev['publishing-{0}-term'.format(self.name)]
        usr1ev = self.ev['publishing-{0}-usr1'.format(self.name)]
        lck = self.ev['publishing-{0}-lck'.format(self.name)]
        evgup = self.ev['publishing-{0}-giveup'.format(self.name)]

        while True:
            try:
                if termev.is_set():
                    self.log.warning('Process {0} received SIGTERM'.format(self.name))
                    lck.acquire(True)
                    self.stats()
                    self.publisher.stats()
                    self.cleanup()
                    lck.release()
                    termev.clear()
                    raise SystemExit(0)

                if usr1ev.is_set():
                    self.log.info('Process {0} received SIGUSR1'.format(self.name))
                    lck.acquire(True)
                    self.stats()
                    self.publisher.stats()
                    lck.release()
                    usr1ev.clear()

                if self.consume_dirq_msgs(max(self.bulk, self.rate)):
                    ret, published = self.publisher.write(self.bulk)
                    if ret:
                        self.remove_dirq_msgs()
                    elif published:
                        self.log.error('{0} {1} giving up'.format(self.__class__.__name__, self.name))
                        self.stats()
                        self.publisher.stats()
                        self.remove_dirq_msgs(published)
                        self.unlock_dirq_msgs(set(e[0] for e in self.inmemq).difference(published))
                        evgup.set()
                        raise SystemExit(0)
                    else:
                        self.log.error('{0} {1} giving up'.format(self.__class__.__name__, self.name))
                        self.stats()
                        self.publisher.stats()
                        self.unlock_dirq_msgs()
                        evgup.set()
                        raise SystemExit(0)

                if int(datetime.now().strftime('%s')) - self.prevstattime >= self.statseveryhour * 3600:
                    self.stats(reset=True)
                    self.publisher.stats(reset=True)

                time.sleep(decimal.Decimal(1) / decimal.Decimal(self.rate))

            except KeyboardInterrupt:
                self.cleanup()
                raise SystemExit(0)

    def consume_dirq_msgs(self, num=0):
        def _inmemq_append(elem):
            self.inmemq.append(elem)
            self.nmsgs_consumed += 1
            self.sess_consumed += 1
            if num and self.sess_consumed == num:
                self.sess_consumed = 0
                self.seenmsgs.clear()
                return True
        try:
            for name in self.dirq:
                if name in self.seenmsgs:
                    continue
                self.seenmsgs.update([name])
                already_lckd = os.path.exists(self.dirq.get_path(name))
                if not already_lckd and self.dirq.lock(name):
                    if _inmemq_append((name, self.dirq.get_message(name))):
                        return True
                elif already_lckd:
                    if _inmemq_append((name, self.dirq.get_message(name))):
                        return True

        except Exception as e:
            self.log.error(e)

        return False

    def unlock_dirq_msgs(self, msgs=None):
        try:
            msgl = msgs if msgs else self.inmemq
            for m in msgl:
                self.dirq.unlock(m[0] if not isinstance(m, str) else m)
            self.inmemq.clear()
        except (OSError, IOError) as e:
            self.log.error(e)

    def remove_dirq_msgs(self, msgs=None):
        try:
            msgl = msgs if msgs else self.inmemq
            for m in msgl:
                self.dirq.remove(m[0] if not isinstance(m, str) else m)
            self.inmemq.clear()
        except (OSError, IOError) as e:
            self.log.error(e)

def init_dirq_consume(**kwargs):
    log = kwargs['log']
    ev = kwargs['ev']
    evsleep = 2
    consumers = list()

    for k, v in kwargs['conf']['queues'].iteritems():
        kw = dict()

        kw.update({'name': k})
        kw.update({'daemonized': kwargs['daemonized']})
        kw.update({'statseveryhour': kwargs['conf']['general']['statseveryhour']})

        if kwargs['conf']['general']['publishmsgfile']:
            kw.update({'publishmsgfiledir': kwargs['conf']['general']['publishmsgfiledir']})
            kw.update({'publisher': FilePublisher})

        if kwargs['conf']['general']['publishargomessaging']:
            try:
                avsc = open(kwargs['conf']['general']['msgavroschema'])
                kw.update({'schema': avro.schema.parse(avsc.read())})
            except Exception as e:
                log.error(e)
                raise SystemExit(1)

            kw.update({'publisher': MessagingPublisher})
            kw.update({'publishtimeout': kwargs['conf']['general']['publishtimeout']})
            kw.update({'publishretry': kwargs['conf']['general']['publishretry']})

        kw.update(kwargs['conf']['queues'][k])
        kw.update(kwargs['conf']['topics'][k])
        kw.update({'log': kwargs['log']})
        kw.update({'ev': kwargs['ev']})
        kw['ev'].update({'publishing-{0}-lck'.format(k): Lock()})
        kw['ev'].update({'publishing-{0}-usr1'.format(k): Event()})
        kw['ev'].update({'publishing-{0}-term'.format(k): Event()})
        kw['ev'].update({'publishing-{0}-giveup'.format(k): Event()})
        kw.update({'evsleep': evsleep})

        consumers.append(ConsumerQueue(**kw))
        if not kwargs['daemonized']:
            consumers[-1].daemon = True
        consumers[-1].start()

    while True:
        for c in consumers:
            if ev['publishing-{0}-giveup'.format(c.name)].is_set():
                c.terminate()
                c.join(1)
                ev['publishing-{0}-giveup'.format(c.name)].clear()

        if ev['term'].is_set():
            for c in consumers:
                ev['publishing-{0}-term'.format(c.name)].set()
                c.join(1)
            raise SystemExit(0)

        if ev['usr1'].is_set():
            for c in consumers:
                ev['publishing-{0}-usr1'.format(c.name)].set()
            ev['usr1'].clear()

        try:
            time.sleep(evsleep)
        except KeyboardInterrupt:
            for c in consumers:
                c.join(1)
            raise SystemExit(0)
