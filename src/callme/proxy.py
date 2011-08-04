
from kombu import BrokerConnection, Exchange, Queue, Consumer, Producer
import uuid
import logging
import pickle
import socket
from kombu.utils import gen_unique_id

from protocol import RpcRequest
from protocol import RpcResponse
from protocol import MyException

class Proxy(object):
	
	timeout = 0
	response = None
	
	def __init__(self,
				server_id = None,
				amqp_host='localhost', 
				amqp_user ='guest',
				amqp_password='guest',
				amqp_vhost='/',
				ssl=False):
		
		self.logger = logging.getLogger('callme.proxy')
		self.timeout = 0
		self.is_received = False
		self.connection = BrokerConnection(hostname=amqp_host,
                              userid=amqp_user,
                              password=amqp_password,
                              virtual_host=amqp_vhost)
		channel = self.connection.channel()
		
		target_exchange = Exchange("callme_target", "direct", durable=False)	
		self.reply_id = gen_unique_id()
		self.logger.debug("Queue ID: %s" %self.reply_id)
		src_exchange = Exchange("callme_src", "direct", durable=False)
		src_queue = Queue(self.reply_id, exchange=src_exchange, 
						routing_key=self.reply_id, auto_delete=True,
						durable=False)
		
		# must declare in advance so reply message isn't
   		# published before.
		src_queue(channel).declare()
		
		
		self.producer = Producer(channel=channel, exchange=target_exchange)
		
		consumer = Consumer(channel=channel, queues=src_queue, callbacks=[self.on_response])
		consumer.consume()		
		
	def on_response(self, body, message):
		
		if self.corr_id == message.properties['correlation_id'] and \
			isinstance(body, RpcResponse):
			self.response = body
			self.is_received = True
			message.ack()
		
	def use_server(self, server_id, timeout=0):
		self.server_id = server_id
		self.timeout = timeout
		return self
	
	
	def __request(self, methodname, params):
		"""
		The remote-method-call is packed into a message and the message is stored in a sending-queue.
		A PublisherThread sends the messages to the AMQPServer.
		This function waits, until a result from the CallMeServer arrives.

		:param methodname: name of the method that should be executed on the CallMeServer
		:param params: parameter for the remote-method-call
		:type methodname: string
		:type param: list of parameters
		:rtype: result received from CallMeServer
		"""
		self.logger.debug('Request: ' + repr(methodname) + '; Params: '+ repr(params))
		
		def panic():
			print "PANIC"
			self.connection.ioloop.stop()
		
		rpc_req = RpcRequest(methodname, params)
		self.corr_id = str(uuid.uuid4())
		self.logger.debug('RpcRequest build')
		self.logger.debug('corr_id: %s' % self.corr_id)
		self.producer.publish(rpc_req, serializer="pickle",
							reply_to=self.reply_id,
							correlation_id=self.corr_id,
							routing_key=self.server_id)
		self.logger.debug('Producer published')
		
		self._wait_for_result()
		
		if self.response.exception_raised:
			raise self.response.result
		
		self.logger.debug('Result: %s' % repr(self.response.result))
		res = self.response.result
		self.response.result = None
		self.is_received = False
		return res
		
	def _wait_for_result(self):
		seconds_elapsed = 0
		while not self.is_received:
			try:
				self.logger.debug('drain events... timeout=%d, counter=%d' 
								% (self.timeout, seconds_elapsed))
				self.connection.drain_events(timeout=1)
			except socket.timeout:
				if self.timeout > 0:
					seconds_elapsed = seconds_elapsed + 1
					if seconds_elapsed > self.timeout:
						raise socket.timeout()

	def __getattr__(self, name):
		"""
		This method is invoked, if a method is being called, which doesn't exist on Proxy.
		It is used for RPC, to get the function which should be called on the Server.
		"""
		# magic method dispatcher
		self.logger.debug('Recursion: ' + name)
		return _Method(self.__request, name)
	
#===========================================================================

class _Method:
	"""
	The _Method-class is used to realize remote-method-calls.
	:param send: name of the function that should be executed on Proxy
	:param name: name of the method which should be called on the Server
	"""
	# some magic to bind an XML-RPC method to an RPC server.
	# supports "nested" methods (e.g. examples.getStateName)
	def __init__(self, send, name):
		self.__send = send
		self.__name = name
	def __getattr__(self, name):
		return _Method(self.__send, "%s.%s" % (self.__name, name))
	def __call__(self, * args):
		return self.__send(self.__name, args)

#===========================================================================