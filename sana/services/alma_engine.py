import time, math

class ALMAEngine:
    def __init__(self):
        self.ocean = {'O': 0.6, 'C': 0.4, 'E': 0.8, 'A': 0.7, 'N': 0.6}
        self.baseline_pad = {
            'P': max(-1.0, min(1.0, 0.21*self.ocean['E'] + 0.59*self.ocean['A'] - 0.19*self.ocean['N'])),
            'A': max(-1.0, min(1.0, 0.15*self.ocean['O'] + 0.30*self.ocean['A'] - 0.57*self.ocean['N'])),
            'D': max(-1.0, min(1.0, 0.25*self.ocean['O'] + 0.17*self.ocean['C'] + 0.60*self.ocean['E'] - 0.32*self.ocean['A']))
        }
        self.decay_rate = 0.02 + 0.05 * (1 - self.ocean['N'])
        self.sensitivity = {'P': 0.3 + 0.5*self.ocean['E'] + 0.2*self.ocean['N'], 'A': 0.3 + 0.3*self.ocean['E'] + 0.4*self.ocean['N'], 'D': 0.4 + 0.4*self.ocean['E'] - 0.2*self.ocean['A']}
        self.current_mood = self.baseline_pad.copy()
        self.last_update_time = time.time()
        self.max_decay_step_minutes = 5.0
        self.emotion_to_pad_impact = {"Joy": (0.6, 0.3, 0.2), "Distress": (-0.6, -0.2, -0.6), "Anger": (-0.7, 0.7, 0.6), "Admiration": (0.4, 0.1, -0.5), "Reproach": (-0.4, 0.4, 0.6), "Neutral": (0.0, 0.0, 0.0)}
        self.current_transient_emotion = "Neutral"
        self.emotion_intensity = 0.0
        self.emotion_stickiness = 0.3
        self.emotion_decay_rate = 0.2

    def process_event(self, labels, intensity=0.5):
        if isinstance(labels, str):
            labels = [labels]
        self.current_transient_emotion = "+".join(labels)
        self.emotion_intensity = max(self.emotion_intensity, intensity)
        self._apply_time_decay()
        ti = [0.0, 0.0, 0.0]
        for l in labels:
            b = self.emotion_to_pad_impact.get(l, (0,0,0))
            m = self._modulate(b)
            ti[0] += m[0]; ti[1] += m[1]; ti[2] += m[2]
        if "Joy" in labels and "Distress" in labels:
            ti[1] += 0.5
        self.current_mood['P'] = max(-1.0, min(1.0, self.current_mood['P'] + ti[0]*intensity))
        self.current_mood['A'] = max(-1.0, min(1.0, self.current_mood['A'] + ti[1]*intensity))
        self.current_mood['D'] = max(-1.0, min(1.0, self.current_mood['D'] + ti[2]*intensity))

    def _apply_time_decay(self):
        now = time.time()
        d = min((now - self.last_update_time)/60.0, self.max_decay_step_minutes)
        self.last_update_time = now
        decay = math.exp(-self.decay_rate * d)
        for dim in ['P','A','D']:
            self.current_mood[dim] = self.baseline_pad[dim] + (self.current_mood[dim] - self.baseline_pad[dim]) * decay
        self.emotion_intensity *= math.exp(-self.emotion_decay_rate * d)
        if self.emotion_intensity < 0.05:
            self.emotion_intensity = 0.0

    def _modulate(self, base):
        p,a,d = self.current_mood['P'], self.current_mood['A'], self.current_mood['D']
        pm = 1.0 - 0.5*p if base[0] > 0 else 1.0 + 0.5*p
        am = 1.0 - 0.3*a if abs(base[1]) > 0.3 else 1.0
        dm = 1.0 + 0.4*d if base[2] > 0 else 1.0 - 0.4*d
        return (base[0]*pm*self.sensitivity['P'], base[1]*am*self.sensitivity['A'], base[2]*dm*self.sensitivity['D'])

    def get_alma_prompt(self):
        p,a,d = self.current_mood['P'], self.current_mood['A'], self.current_mood['D']
        return f"[ALMA] P={p:.2f} A={a:.2f} D={d:.2f} emotion={self.current_transient_emotion}"

    def _describe_mood(self, p, a, d):
        if p > 0.4 and a > 0.4: return 'happy, energetic'
        if p < -0.4 and a < -0.2: return 'down, tired'
        if abs(p) < 0.3 and abs(a) < 0.3: return 'calm, neutral'
        return 'mixed mood'
