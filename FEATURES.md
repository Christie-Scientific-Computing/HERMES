## Features
 - [ ] Make MU tolerance (in PinnacleExport) configurable from the UI. Perhaps need an advanced options tab when submitting a job (which contains MU tolerance + message ID, only people who know what they're doing should tweak these).
 - [ ] Allow users to change their own passwords. Clicking on username in navbar should take you to user page. Add a change password form here (can we reuse the user verification password page?)
 - [ ] Endpoint: /jobs/{job-id}/patients/{patient-id}/

 ```
    Current UI:

    When | Stage | Event | Attempt | Error
    2026-08-17T13:03:22.612204+00:00 | retrieve | start | 1 |  
    2026-08-17T13:03:26.345775+00:00 | retrieve | failure | 1 | InvalidPathError('tmp/proknow is invalid')

    Suggest changing to:

    When | Execution Time| Stage | Event | Attempt | Error
    2026-08-17 at 13:03:26 | 00:00:04| retrieve | failure | 1 | InvalidPathError('tmp/proknow is invalid')
```
 - [ ] Some high-level info about a project should be shown on the project homepage (/projects/{project-id}). Basic info like X patients requested, Y patients restored and Z patients sent to DESTINATION. Project expiration date should be highlighted. There is a lot of whitespace in the "Approval Status" Div. Maybe add some key figures there? i.e. two smaller divs side by side (Approval status + Project overview).
 
 - [ ] Add some fields to project creation / approval page. Description should be a mandatory field. Add export destination to the project page (i.e. users need to specify where the data is going and this needs to be reviewed). Job submission page should also update to show this value (unchangeable) when specifying an export job. Users should be able to upload a list of patient IDs (optional). Not uploading patient list, should show a warning suggesting users do this (but should not block them).

 - [ ] Implement an error reporting/suggestions page. Should handle general suggestions, improvements, flagging data leaks (e.g. patient ID shown in error message). Allow users to mark category of issue (feedback vs error).
Also add email address for more urgent issues (I will do this myself). Could also add a log for admins to view. 
