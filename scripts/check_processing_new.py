#!/usr/bin/python


# clone of monitor.py to do LB target processing
from time import sleep
import datetime
from surveys_db import SurveysDB, tag_field, get_cluster

import os
import glob
import requests
import stager_access
from rclone import RClone   ## DO NOT pip3 install --user python-rclone -- use https://raw.githubusercontent.com/mhardcastle/ddf-pipeline/master/utils/rclone.py
from download_file import download_file ## in ddf-pipeline/utils
#import progress_bar
from sdr_wrapper import SDR
from reprocessing_utils import do_sdr_and_rclone_download, do_rclone_download
from tasklist import get_task_list, mark_done
import numpy as np
from lbfields_utils import (
    update_status,
    get_local_obsid,
    collect_solutions_lhr,
    run_task,
    stage_field,
    do_download,
    do_unpack,
    check_field,
    cleanup_step,
    do_verify,
    archive_lbfield,
    get_thread_status,
    print_thread_status,
)


#################################
## CLUSTER SPECIFICS - use environment variables

'''
PLEASE SEE THE slurm/add_these_to_bashrc.txt file
'''

user = os.getenv('USER')
if len(user) > 20:
    user = user[0:20]
cluster = os.getenv('DDF_PIPELINE_CLUSTER')
if cluster is None:
    raise RuntimeError('DDF_PIPELINE_CLUSTER must be set')
softwaredir = os.getenv('SOFTWAREDIR')
basedir = os.getenv('DATA_DIR')
if basedir is None:
    raise RuntimeError('DATA_DIR must point to your data directory')
if not os.getenv('LOFAR_SINGULARITY'):
    raise RuntimeError('LOFAR_SINGULARITY must point to a singularity image')
procdir = os.path.join(basedir,'processing')

totallimit=20
staginglimit=2

## cluster specific queuing limits
if cluster == 'spider':
    maxqueue = 10
elif cluster == 'cosma':
    maxqueue = 3
else:
    #default
    maxqueue = 10

## collect information on what's started 
with SurveysDB(readonly=True) as sdb:
    sdb.cur.execute('select * from lb_fields where clustername="'+cluster+'" and username="'+user+'" order by priority,id')
    result=sdb.cur.fetchall()
    sdb.cur.execute('select * from lb_fields where status="Not started" and priority>0 order by priority,id')
    result2=sdb.cur.fetchall()
    if len(result2)>0:
        nextfield=result2[0]['id']
    else:
        nextfield=None

d={} ## len(fd)
fd={}  ## dictionary of fields of a given status type
for r in result:
    status=r['status']
    if status in d:
        d[status]=d[status]+1
        fd[status].append(r['id'])
    else:
        d[status]=1
        fd[status]=[r['id']]
d['Not started']=len(result2)
print('\n\n-----------------------------------------------\n\n')
print('LB target reprocessing status on cluster %s' % (cluster))
print(datetime.datetime.now())
print()
failed=0
for k in sorted(d.keys()):
    print("%-20s : %i" % (k,d[k]))
    if 'ailed' in k:
        failed+=d[k]

## print out the information
print()
ksum=len(glob.glob(basedir+'/P*+*'))#-failed
if ksum<0: ksum=0
print(ksum,'live directories out of',totallimit)
print('Next field to work on is',nextfield)

print(d)
print(fd)
