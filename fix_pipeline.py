import os

def fix_file(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
        
    for i, line in enumerate(lines):
        if 'valid_anchors = [a for a in unique_anchors' in line:
            indent = line[:len(line) - len(line.lstrip())]
            lines[i] = f'{indent}valid_anchors = []\n{indent}if unique_anchors:\n{indent}    valid_anchors = [a for a in unique_anchors if a and a != "【PRD未说明】"]\n'
        elif 'valid_anchors = [a for a in anchors_list' in line:
            indent = line[:len(line) - len(line.lstrip())]
            lines[i] = f'{indent}valid_anchors = []\n{indent}if anchors_list:\n{indent}    valid_anchors = [a for a in anchors_list if a and a != "【PRD未说明】"]\n'

    with open(file_path, 'w', encoding='utf-8') as f:
        f.writelines(lines)
    print(f'Fixed {file_path}')

fix_file('d:/trae-code/trae_platform/modules/prd_audit/pipeline.py')
fix_file('d:/trae-code/trae_platform/modules/prd_audit_clone/pipeline.py')
fix_file('d:/trae-code/trae_platform/modules/prd_audit/pipeline_v6_backup.py')
