//! Pure descriptor evidence and allowlisted AAP frame shape analysis.



const SENSOR_FRAMEWORK_MARKERS: [&[u8]; 4] = [
    b"AccessoryService",
    b"devmotion6",
    b"MaxReportSize",
    b"ReportDescriptor",
];
const HEART_RATE_SERVICE_MARKER: &[u8] = b"HeartRateService";
const HEART_RATE_ACCESS_MARKER: &[u8] = b"com.apple.hid.heartrate-access";
const HEART_RATE_MARKER: &[u8] = b"HeartRate";




#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct DescriptorEvidence {
    pub sensor_framework: bool,
    pub heart_rate_service: bool,
    pub heart_rate: bool,
    pub heartrate_access: bool,
}

impl DescriptorEvidence {
    #[must_use]
    pub fn merged(self, frame: &[u8]) -> Self {
        Self {
            sensor_framework: self.sensor_framework
                || SENSOR_FRAMEWORK_MARKERS
                    .iter()
                    .any(|marker| contains(frame, marker)),
            heart_rate_service: self.heart_rate_service
                || contains(frame, HEART_RATE_SERVICE_MARKER),
            heart_rate: self.heart_rate || contains_standalone_heart_rate(frame),
            heartrate_access: self.heartrate_access || contains(frame, HEART_RATE_ACCESS_MARKER),
        }
    }
}

fn contains(frame: &[u8], marker: &[u8]) -> bool {
    frame.windows(marker.len()).any(|window| window == marker)
}

fn contains_standalone_heart_rate(frame: &[u8]) -> bool {
    frame
        .windows(HEART_RATE_MARKER.len())
        .enumerate()
        .any(|(start, window)| {
            window == HEART_RATE_MARKER
                && (start == 0 || !frame[start - 1].is_ascii_alphanumeric())
                && (start + HEART_RATE_MARKER.len() == frame.len()
                    || !frame[start + HEART_RATE_MARKER.len()].is_ascii_alphanumeric())
        })
}











#[cfg(test)]
mod tests {
    use super :: * ;

    

    #[test]
    fn descriptor_markers_accumulate_and_remain_monotonic() {
        for marker in SENSOR_FRAMEWORK_MARKERS {
            assert!(
                DescriptorEvidence::default()
                    .merged(marker)
                    .sensor_framework
            );
        }
        let evidence = DescriptorEvidence::default()
            .merged(b"AccessoryService")
            .merged(b"HeartRateService")
            .merged(b"com.apple.hid.heartrate-access")
            .merged(b"_HeartRate_")
            .merged(b"unrelated");
        assert_eq!(
            evidence,
            DescriptorEvidence {
                sensor_framework: true,
                heart_rate_service: true,
                heart_rate: true,
                heartrate_access: true
            }
        );
        assert!(
            !DescriptorEvidence::default()
                .merged(b"unrelated")
                .sensor_framework
        );
    }

    #[test]
    fn heart_rate_ascii_boundaries_match_python_regex() {
        for value in [
            b"HeartRate".as_slice(),
            b"\0HeartRate\0",
            b"_HeartRate_",
            b"-HeartRate-",
            b"HeartRate!",
            b"!HeartRate",
            b"\xffHeartRate\xff",
        ] {
            assert!(
                DescriptorEvidence::default().merged(value).heart_rate,
                "{value:?}"
            );
        }
        for value in [
            b"xHeartRate".as_slice(),
            b"HeartRate2",
            b"AHeartRateB",
            b"9HeartRate",
            b"HeartRatez",
            b"HeartRateService",
        ] {
            assert!(
                !DescriptorEvidence::default().merged(value).heart_rate,
                "{value:?}"
            );
        }
        assert!(
            DescriptorEvidence::default()
                .merged(b"xHeartRate HeartRate2 _HeartRate")
                .heart_rate
        );
    }

    

    

    

    
}
