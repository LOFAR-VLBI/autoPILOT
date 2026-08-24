#!/usr/bin/python3


# clone of monitor.py to do LB target processing
# from time import sleep
# import datetime
from surveys_db import SurveysDB, tag_field, get_cluster
import sys
import os
# import glob
import requests
# import stager_access
# from rclone import RClone   ## DO NOT pip3 install --user python-rclone -- use https://raw.githubusercontent.com/mhardcastle/ddf-pipeline/master/utils/rclone.py
# from download_file import download_file ## in ddf-pipeline/utils
#import progress_bar
# from sdr_wrapper import SDR
# from reprocessing_utils import do_sdr_and_rclone_download, do_rclone_download
# from tasklist import get_task_list, mark_done
# import numpy as np
# from lbfields_utils import (
#     update_status,
#     get_local_obsid,
#     collect_solutions_lhr,
#     run_task,
#     stage_field,
#     do_download,
#     do_unpack,
#     check_field,
#     cleanup_step,
#     do_verify,
#     archive_lbfield,
#     get_thread_status,
#     print_thread_status,
# )


#################################
## CLUSTER SPECIFICS - use environment variables

'''
PLEASE SEE THE slurm/add_these_to_bashrc.txt file
'''


## collect information on what's started 
with SurveysDB(readonly=True) as sdb:
    sdb.cur.execute('select * from lb_fields where id="'+sys.argv[1]+'" order by priority,id')
    result=sdb.cur.fetchall()

for r in result:
    print(r)
    