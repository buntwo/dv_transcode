Directories
-----------
- Originals: raw DV captures, ~13GB/hr
- Access: transcoded access copies, with post processing.
- Logs: logs, transcode commands, Digital8 subtitle files for timestamp overlays
- Splits: DV files split according to logical breaks, still in raw DV codec.
- scripts: code


Digital8 files
--------------
Digital 8 files have recording time embedded, so we can auto split them based on that. Here are the steps I take:
1. View the dv file in dvrescue GUI to see the splits. Typically this is too many splits.
2. Use dv_unpackager.py to run the split, which logs the command for archival purposes.
3. Use dv_unpackager.py to unsplit, which logs the command for archival purposes.
4. Transcode the resulting out_partN.dv files using transcode3.py
