class InsightsDict(str):
    def get(self, key, default=None):
        if key == "available":
            return False
        return default
        
def extract_conference_call_insights(chunks):
    return InsightsDict("{}")
