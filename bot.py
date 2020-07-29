from __future__ import print_function
from __future__ import unicode_literals

import os
import re
import sys
import json, ast
import time as t
import threading
import configuration
from thehive_sla_monitor.logger import logging

from multiprocessing import Process, Queue, Manager
from flask import Flask, request, make_response, redirect
from datetime import datetime, time
from slack import WebClient
from twilio.rest import Client
from thehive4py.api import TheHiveApi
from thehive4py.query import *
from thehive_sla_monitor.templates import slack_bot_alert_notice_template, slack_bot_alert_notice_update, slack_bot_alert_notice_ignore

# Defined SLA Limits in int: seconds
sla_30 = 1800
sla_45 = 2700
sla_60 = 3600

hive_30_list = []
hive_30_dict = {}
hive_45_list = []
hive_45_dict = {}
hive_60_list = []
hive_60_dict = {}
ignore_list = []
called_list = []

# Instantiate Slack & Twilio variables
slack_webhook_url = configuration.SLACK_SETTINGS['SLACK_WEBHOOK_URL']
twimlet = configuration.TWILIO_SETTINGS['TWIMLET_URL']

# Initialize API classes
slack_client = WebClient(configuration.SLACK_SETTINGS['SLACK_APP_TOKEN'])
twilio_client = Client(configuration.TWILIO_SETTINGS['ACCOUNT_SID'], configuration.TWILIO_SETTINGS['AUTH_TOKEN'])
api = TheHiveApi("http://%s:%s" % (configuration.SYSTEM_SETTINGS['HIVE_SERVER_IP'], configuration.SYSTEM_SETTINGS['HIVE_SERVER_PORT']), configuration.SYSTEM_SETTINGS['HIVE_API_KEY'])
channel = configuration.SLACK_SETTINGS['SLACK_CHANNEL']

# Instatiate Flask classes
app = Flask(__name__)
manager = Manager()
alert_dict = manager.dict()

def create_hive30_dict(id, rule_name, alert_date, alert_age, *args):
  logging.info("Updating 30M SLA dictionary")
  hive_30_dict.update({ id : rule_name, })
  send_sms(*args)
  slack_bot_notice_alert(channel, id, rule_name, alert_date, alert_age)
  send_sms(*args)

def create_hive45_dict(id, rule_name, alert_date, alert_age, *args):
  logging.info("Updating 45M SLA dictionary")
  hive_45_dict.update({ id : rule_name, })
  slack_bot_notice_alert(channel, id, rule_name, alert_date, alert_age)
  make_call(id)

def create_hive60_dict(id, rule_name, alert_date, alert_age, *args):
  hive_60_dict.update({ id : rule_name, })
  slack_bot_notice_alert(channel, id, rule_name, alert_date, alert_age)
  logging.info(' Alert ID: ' + id + ' Rule Name: ' + rule_name + ' Alert Date: ' + alert_date + ' Alert Age: ' + alert_age)
  # Escalate to senior - Create a list of multiple numbers and iterate through list to call them!

def add_to_30m(id):
  if id in hive_30_list:
    logging.info('Already added - 30 minute SLA list: ' + id)
  else:
    logging.info('Appending - 30 minute SLA list: ' + id)
    hive_30_list.append(id)

def add_to_45m(id):
  if id in hive_45_list:
    logging.info('Already added - 45 minute SLA list: ' + id)
  else:
    logging.info('Appending - 45 minute SLA list: ' + id)
    hive_45_list.append(id)

def add_to_60m(id):
  if id in hive_60_list:
    logging.info('Already added - 60 minute SLA list: ' + id)
  else:
    logging.info('Appending - 60 minute SLA list: ' + id)
    hive_60_list.append(id)

def clean_ignore_list(id):
  t.sleep(3600)
  logging.info("Removing " + id + " from ignore list")
  ignore_list.remove(id)

def add_to_temp_ignore(id):
  logging.info("Adding " + id + " to ignore list")
  ignore_list.append(id)
  ignore_thread = threading.Thread(target=clean_ignore_list)
  ignore_thread.start()
  return id

def add_to_called_list(id):
  logging.info("Adding " + id + " to called list")
  called_list.append(id)
  return id

def is_empty(any_structure):
    if any_structure:
        return False
    else:
        return True

def search(title, query):
  current_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
  current_date = datetime.strptime(current_date, '%Y-%m-%d %H:%M:%S')
  response = api.find_alerts(query=query)

  if response.status_code == 200:
    data = json.dumps(response.json())
    jdata = json.loads(data)
    for d in jdata:
      if d['severity'] == 3:
        ts = int(d['createdAt'])
        ts /= 1000
        alert_date = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
        logging.info("Found: " + d['id'] + " " + d['title'] + " " + str(alert_date))
        alert_date = datetime.strptime(alert_date, '%Y-%m-%d %H:%M:%S')
        diff = (current_date - alert_date)
        if sla_30 < diff.total_seconds() and sla_45 > diff.total_seconds():
          logging.info("30/M SLA: " + str(diff) + " " + d['id'])
          create_hive30_dict(d['id'], d['title'], str(alert_date), str(diff), d)
          add_to_30m(d['id'])
        elif sla_45 < diff.total_seconds() and sla_60 > diff.total_seconds():
          logging.info("45/M SLA: " + str(diff) + " " + d['id'])
          create_hive45_dict(d['id'], d['title'], str(alert_date), str(diff), d)
          add_to_45m(d['id'])
        elif sla_60 < diff.total_seconds():
          logging.info("60/M SLA: " + str(diff) + " " + d['id'])
          create_hive60_dict(d['id'], d['title'], str(alert_date), str(diff), d)
          add_to_60m(d['id'])

    logging.info('')

  else:
    logging.info('ko: {}/{}'.format(response.status_code, response.text))

def send_sms(*args):
  for alert_id, alert_name in hive_30_dict.items():
    if alert_id in hive_30_list:
      logging.warning("Already sent SMS regarding ID: " + alert_id)
    else:
      if not is_empty(args):
        for alert_data in args:
          alert_dump = json.dumps(alert_data, indent=2)
        #  logging.info(alert_dump) # If you want to see all items in Hive response
          alert_json = json.loads(alert_dump)

          severity = (alert_json['severity']) # I want severity
          all_artifacts = (alert_json['artifacts']) # I want all artifacts
          data_artifacts = (alert_json['artifacts'][0]['data']) # I want only the data item which lies under the artifacts element

          msg_body = severity # Send only the severity
       #   msg_body = severity + " " + data_artifacts # Send severity and data_artifacts

      else:
        msg_body = 'Please attend immediately!'

      twilio_msg_body = 'TheHive SLA Alert - %s \n %s' % (alert_id, msg_body)
      message = twilio_client.messages.create(body=twilio_msg_body, from_=configuration.TWILIO_SETTINGS['TWILIO_SENDER'], to=configuration.TWILIO_SETTINGS['TWILIO_RTCP'])
      t.sleep(2)
      logging.info('SMS Sent: ' + str(message.sid))
      try:
        add_to_30m(alert_id)
      except Exception as re:
        logging.error("Error when adding to list after sending SMS: " + str(re))

def make_call(id):
  if id in called_list:
    logging.info("User has been called regarding ID: " + id)
  else:
    call = twilio_client.calls.create(url=twimlet, to='$insert_phone_number', from_=twilio_number)
    logging.info("Call Sent: " + str(call.sid))
    try:
      add_to_called_list(id)
    except Exception as error:
      logging.error("Error when adding to list after making call: " + str(error))

def slack_bot_notice_alert(channel, id, rule_name, alert_date, alert_age):
  for k,v in hive_30_dict.items():
    if k in hive_30_list or k in ignore_list:
      logging.warning("Already notified regarding ID / Previously ignored: " + k)
    else:
      res = slack_client.chat_postMessage(channel=channel, text="TheHive SLA Monitor: SLA Breach", blocks=slack_bot_alert_notice_template(id, rule_name, alert_date, alert_age))
      alert_dict[id] = {'channel': res['channel'],'ts': res['message']['ts'], 'rule_name': rule_name, 'alert_date': alert_date, 'alert_age': alert_age }
  for k,v in hive_45_dict.items():
    if k in hive_45_list or k in ignore_list:
      logging.warning("Already notified regarding ID / Previously ignored: " + k)
    else:
      res = slack_client.chat_postMessage(channel=channel, text="TheHive SLA Monitor: SLA Breach", blocks=slack_bot_alert_notice_template(id, rule_name, alert_date, alert_age))
      alert_dict[id] = {'channel': res['channel'],'ts': res['message']['ts'], 'rule_name': rule_name, 'alert_date': alert_date, 'alert_age': alert_age }
  for k,v in hive_60_dict.items():
    if k in hive_60_list or k in ignore_list:
      logging.warning("Already notified regarding ID / Previously ignored: " + k)
    else:
      res = slack_client.chat_postMessage(channel=channel, text="TheHive SLA Monitor: SLA Breach", blocks=slack_bot_alert_notice_template(id, rule_name, alert_date, alert_age))
      alert_dict[id] = {'channel': res['channel'],'ts': res['message']['ts'], 'rule_name': rule_name, 'alert_date': alert_date, 'alert_age': alert_age }

def promote_to_case(case_id):
  logging.info("Promoting Alert " + case_id)
  response = api.promote_alert_to_case(case_id)
  if response.status_code == 201:
    data = json.dumps(response.json())
    jdata = json.loads(data)
    case_id = (jdata['id'])
    return case_id
  else:
    logging.error('ko: {}/{}'.format(response.status_code, response.text))

@app.route("/complete/<id>", methods=['GET', 'POST'])
def complete(id):
  case_id = promote_to_case(id)
  logging.info("ID: " + id + " Case ID: " + case_id)

  res = slack_client.chat.update(channel=alert_dict[id]['channel'], ts=alert_dict[id]["ts"], text="TheHive SLA Monitor: Case Promoted", blocks=slack_bot_alert_notice_update(id, alert_dict[id]['rule_name'], alert_dict[id]['alert_date'], alert_dict[id]['alert_age']))
  res = slack_client.chat.getPermalink(channel=alert_dict[id]["channel"],message_ts=alert_dict[id]["ts"])

  hive_link = "https://%s/hive/index.html#/case/{}/details" % (configuration.SYSTEM_SETTINGS['HIVE_URL'], format(case_id))
  return redirect(hive_link, code=302)

@app.route("/ignore/<id>", methods=['GET', 'POST'])
def ignore(id):
  add_to_temp_ignore(id)
  ignore_id = add_to_temp_ignore(id)
  logging.info("Ignoring ID: " + ignore_id)

  res = slack_client.chat.update(channel=alert_dict[id]['channel'], ts=alert_dict[id]["ts"], text="TheHive SLA Monitor: Case Ignored", blocks=slack_bot_alert_notice_ignore(id, alert_dict[id]['rule_name'], alert_dict[id]['alert_date'], alert_dict[id]['alert_age']))
  res = slack_client.chat.getPermalink(channel=alert_dict[id]["channel"],message_ts=alert_dict[id]["ts"])

  return ('', 204)

def bot_start():
  while True:
    search('Formatted DATA:', Eq('status', 'New'))
    t.sleep(120)

def webserver_start():
    app.run(port=configuration.SYSTEM_SETTINGS['FLASK_WEBSERVER_PORT'], host=configuration.SYSTEM_SETTINGS['FLASK_WEBSERVER_IP'])
    return

if __name__ == '__main__':
  webserver_start = Process(target=webserver_start)
  bot_start = Process(target=bot_start)
  bot_start.start()
  webserver_start.start()
  bot_start.join()
  webserver_start.join()
