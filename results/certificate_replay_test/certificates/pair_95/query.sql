SELECT
  a1.firstname,
  a1.lastname,
  a2.city,
  a2.state
FROM PERSON AS A1
LEFT JOIN ADDRESS AS A2
  ON a1.personid = a2.personid