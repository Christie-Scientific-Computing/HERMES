### Adressing Github Issues (09/09)

1. Your order works well.
2. 'mu_tolerance' is a float passed to PinnacleExport via payload in backend.src.retrieve.logic.import_from_pinnacle (lines 303-313). Message ID is the user-defined field (message_id in frontend_fastapi.forms.jobs.py -- line 110), nothing to change here it just needs grouping with mu_tolerance in advanced options. 
3. Just visually de-emphasised, collapsed by default anyone can open. Setting message_id should raise warning. 
4. Message ID can be set per-project but Mu tolerance is set per-job. I.e. message_id auto-fills if it is defined in project. MU tolerance should be set to default for every job, unless user manually changes prior to job submission.
5. I have migrated away from django in favour of fastapi. Yes, users should have to specify their old password.
6. Just a change password form for now
7. I think you misunderstood, I want to remove start/completion rows and merge them into one with a start time + end time (or In progress/--). So there should be start/end rows, rather start/end columsn per job.
8. Color-coded.
9. No placeholder version
10. Leave as-is
11. Same dynamic list + Proknow (specify collection(s) if this is the case). User can select multiple options if needed. Should be approved in the normal approve/reject decision. Can be changed via project amendment but amendment also needs approval (i.e. amendment re-opens the approval state)
12. Yes your solution works. 
13. Yep agree with this, backend db should record this.
14. Require login, auto-attach username. Users should also be able to specify the related job-id (optional). As well as a free text field to write issue.
15. Yep, I like your approach
16. Reuse _is_data_custodian 


####

Regarding Deferred email question: the frontend doesn't have SMTP set up and the send_email() doesn't work. This is beyond the scope of the current implementation. 

17. Yes env var is fine.
18. No, it should require approval as it is used as a trigger for different trial anonymisation tables. 
19. It should log user out of all sessions. 
20. Yes, stay live on current version until approval
21. Split by destination, someone should know immediately where the data was sent. Although this should only report unique patient IDs.
22. Yes that looks good.

I have an additional feature I'd like to implement.
What information can the jobs table on the frontpage display. I don't think the information provided currently is useful. The UIDs + filepaths also clutter the screen. Propose some alternatives please
  

#### Local PACS query
1. It's a separate PACS, B is spot-on. It's a Conquest DICOM server if that helps

2. Users can browse, admins/data custodians can move.

3. Standalone page, your answer is correct.

-- 
1. This is going to exist on a separate machine without an Orthanc Server, it's just the frontend.Is this still possible? In-browser DICOM request? 

2. Data-custodian is enough 

3. Patient + study level -> Expands into series level

4. Could we implement both? 


