Pod::Spec.new do |s|
  s.name           = 'AsteriskBridge'
  s.version        = '1.0.0'
  s.summary        = 'App Group access and the broadcast picker for Asterisk'
  s.description    = 'App Group access and the broadcast picker for Asterisk'
  s.author         = ''
  s.homepage       = 'https://github.com/DylPorter/asterisk'
  s.platforms      = { :ios => '17.0' }
  s.source         = { git: '' }
  s.static_framework = true
  s.dependency 'ExpoModulesCore'
  s.frameworks     = 'ReplayKit'
  s.pod_target_xcconfig = { 'DEFINES_MODULE' => 'YES' }
  s.source_files = "**/*.{h,m,mm,swift}"
end
