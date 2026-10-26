"""独立历史镜像：修订号单调，样本和episode允许向后定位。"""
from copy import deepcopy


class ReplayState:
    def __init__(self, source: dict, generation: int):
        self.source = {key: source[key] for key in ('catalog_id', 'run_id', 'trajectory_artifact_id')}
        self.generation = generation
        self.session_id = None
        self.revision = -1
        self.data = None
        self.requested_command = None
        self.selected_object = None
        self.pairing = {}
        self.recording = None
        self.error = None

    @property
    def cursor(self):
        return self.data['cursor'] if self.data else None

    def apply(self, message: dict, *, generation: int) -> bool:
        if (generation != self.generation
                or message['source'] != {key: self.source[key] for key in ('run_id', 'trajectory_artifact_id')}
                or self.session_id is not None and message['session_id'] != self.session_id
                or message['revision'] <= self.revision):
            return False
        self.session_id, self.revision = message['session_id'], message['revision']
        self.data = deepcopy(message)
        if self.requested_command and message['last_command_seq'] >= self.requested_command['command_seq']:
            self.requested_command = None
        return True

    def object_diagnostics(self):
        """只投影当前已应用frame的role_slot，不误用group_slot或上一个样本。"""
        frame = self.data['diagnostics'] if self.data else None
        if frame is None or self.selected_object is None:
            return None
        pose = frame['objects'][self.selected_object]
        role_id, slot = pose['role_id'], pose['role_slot']
        role = None
        if role_id is not None:
            values = frame['roles'][role_id]
            action = values['action']
            if action is not None:
                action = {key: ({name: array[slot] for name, array in value.items()} if key == 'radio'
                                else value[slot]) for key, value in action.items()}
            role = {key: values[key][slot] for key in ('velocity_world', 'angular_velocity_body',
                    'present', 'alive', 'controllable', 'crashed')}
            role.update(action=action, reward=values['reward'][slot] if values['reward'] is not None else None,
                        costs={key: value[slot] for key, value in values['costs'].items()} if values['costs'] is not None else None,
                        learning_done=values['learning_done'][slot] if values['learning_done'] is not None else None)
        return dict(sample_index=frame['sample_index'], world_id=frame['world_id'], episode=frame['episode'],
                    decision=frame['decision'], tick=frame['tick'], sim_time=frame['sim_time'],
                    phase=frame['phase'], reset_source=frame['reset_source'], pose=deepcopy(pose), role=role)
